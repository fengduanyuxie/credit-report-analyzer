import os
import json
import requests
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# TextIn API 凭证（固定）
TEXTIN_APP_ID = "0fd9239e2c07003f28d8262745cd3a92"
TEXTIN_SECRET_CODE = "e87042c286a20aeb61790587432baadd"
TEXTIN_API_URL = "https://api.textin.com/ai/service/v1/pdf_to_markdown"

SYSTEM_PROMPT = """你是一名资深的助贷风控专家，擅长解读征信报告。

请严格按以下要求分析并输出报告：

### 第一部分：简要汇总（严格按以下格式，不添加任何额外说明）

*基本信息
性别：{从身份证第17位判断：奇数为男，偶数为女}
年龄：{报告日期年份 - 出生年份，未过生日减1}
婚姻：{已婚/未婚/未知}

*查询记录
30天内：{30天内贷款审批/信用卡审批次数，排除贷后管理}
31-90天：{31-90天内同上}
90-180天：{90-180天内同上}
180-360天：{180-360天内同上}
60天内小网贷：{60天内小网贷机构查询次数}
60天内本人：{60天内本人查询次数}

*5年内逾期
总月数：{所有账户逾期月数总和}
90天以上的账户数：{发生过90天以上逾期的账户数}

*贷款
机构数：{未结清且有余额的贷款机构数}
总余额：{所有贷款余额总和}万元
小网贷的机构数：{小网贷机构数（含余额为0的）}
小网贷的余额：{小网贷余额总和}万元

*信用卡
机构数：{信用卡机构数（未销户、人民币、额度>0）}
授信额：{信用卡总授信额度}万元
已用额度：{信用卡已用额度}万元
使用率：{使用率}%

### 第二部分：展开分析

请基于以上数据，生成一份专业、详细的征信分析报告，包含以下内容：
1. 基本信息解读
2. 查询记录分析
3. 逾期记录分析
4. 贷款信息分析
5. 信用卡信息分析
6. 综合评估与风控建议（风险等级：正常/关注/次级/可疑/损失）

## 提取规则
1. 小网贷判断：机构名称包含（网商、微众、亿联、金城、裕民、海峡、振兴、新网、苏商、中关村、富民、锡商、百信、长安、兰州、威海、众邦、蓝海、华通、华瑞、友利）或不含"银行"二字
2. 金额单位：统一转换为万元
3. 年龄：使用周岁
4. 值为0的项不显示

## 风险等级判定
- 正常：无逾期、无不良记录、负债合理
- 关注：有少量逾期（<3个月）、负债偏高
- 次级：有30-90天逾期、小网贷使用
- 可疑：有90天以上逾期、呆账、资产处置
- 损失：多重严重负面记录

请分析以下征信报告内容："""


def parse_pdf_with_textin(pdf_bytes: bytes, filename: str) -> str:
    """使用 TextIn API 解析 PDF"""
    headers = {
        "x-ti-app-id": TEXTIN_APP_ID,
        "x-ti-secret-code": TEXTIN_SECRET_CODE,
        "Content-Type": "application/octet-stream"
    }
    
    params = {
        "dpi": 144,
        "parse_mode": "scan",
        "markdown_details": 1,
        "table_flavor": "md",
        "apply_document_tree": 0
    }
    
    response = requests.post(
        TEXTIN_API_URL,
        params=params,
        headers=headers,
        data=pdf_bytes,
        timeout=60
    )
    
    if response.status_code != 200:
        raise Exception(f"TextIn API HTTP错误: {response.status_code} - {response.text[:200]}")
    
    result = response.json()
    if result.get("code") != 200:
        raise Exception(f"TextIn 业务错误: {result.get('code')} - {result.get('message', '未知错误')}")
    
    markdown = result.get("result", {}).get("markdown", "")
    if not markdown:
        raise Exception("TextIn 未返回文本内容")
    
    return markdown


def call_deepseek(text: str) -> str:
    """调用 DeepSeek API 进行分析"""
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:12000]}
        ],
        "temperature": 0
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=120)
    data = response.json()
    return data["choices"][0]["message"]["content"]


@app.post("/api/analyze")
async def analyze(file: UploadFile):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "缺少 DeepSeek API Key")
    
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过10MB")
    
    try:
        extracted_text = parse_pdf_with_textin(pdf_bytes, file.filename)
        if not extracted_text or len(extracted_text) < 50:
            raise HTTPException(400, "PDF 内容提取失败")
    except Exception as e:
        raise HTTPException(500, f"PDF 解析失败: {str(e)}")
    
    try:
        result = call_deepseek(extracted_text)
    except Exception as e:
        raise HTTPException(500, f"大模型分析失败: {str(e)}")
    
    return JSONResponse({"success": True, "full_report": result})


@app.get("/api/health")
def health():
    return {"status": "ok", "parser": "textin"}


@app.get("/")
def frontend():
    html = '''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>征信报告分析系统</title>
<style>
body {font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #f5f7fa;}
.container {background: white; border-radius: 16px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);}
h1 {color: #1e3c72; border-bottom: 2px solid #4a90e2; padding-bottom: 10px;}
.upload-area {border: 2px dashed #4a90e2; border-radius: 12px; padding: 40px; text-align: center; background: #fafcff; margin: 20px 0; cursor: pointer;}
.upload-area:hover {background: #eef4ff;}
input[type="file"] {display: none;}
button {background: #4a90e2; color: white; border: none; padding: 12px 28px; border-radius: 40px; font-size: 16px; cursor: pointer;}
button:hover {background: #357abd;}
button:disabled {background: #b0c4de; cursor: not-allowed;}
.result {background: #f9f9f9; border-radius: 12px; padding: 20px; margin-top: 20px; white-space: pre-wrap; font-family: monospace; font-size: 14px; line-height: 1.5; max-height: 600px; overflow-y: auto;}
.loading {display: none; text-align: center; margin: 20px; color: #4a90e2;}
.error {color: #d32f2f; background: #ffebee; padding: 12px; border-radius: 8px; margin: 20px 0;}
</style>
</head>
<body>
<div class="container">
<h1>📄 个人征信报告AI分析系统</h1>
<p>上传PDF格式的个人信用报告，系统将自动解析并生成专业风控报告。</p>
<div class="upload-area" onclick="document.getElementById('file').click()">
<p>📎 点击或拖拽上传PDF文件</p>
<input type="file" id="file" accept=".pdf">
</div>
<div style="text-align: center;"><button id="analyzeBtn" disabled>开始分析</button></div>
<div class="loading" id="loading">⏳ 正在解析并分析报告，请稍候...（可能需要30-60秒）</div>
<div id="resultContainer" style="display: none;">
<div class="result" id="result"></div>
</div>
</div>
<script>
let selectedFile = null;
const fileInput = document.getElementById('file');
const uploadArea = document.querySelector('.upload-area');
const analyzeBtn = document.getElementById('analyzeBtn');
const loadingDiv = document.getElementById('loading');
const resultContainer = document.getElementById('resultContainer');
const resultDiv = document.getElementById('result');

uploadArea.addEventListener('click', () => fileInput.click());
uploadArea.addEventListener('dragover', (e) => { e.preventDefault(); uploadArea.style.background = '#eef4ff'; });
uploadArea.addEventListener('dragleave', () => { uploadArea.style.background = '#fafcff'; });
uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.style.background = '#fafcff';
    if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
});

fileInput.addEventListener('change', (e) => { if (e.target.files.length > 0) handleFile(e.target.files[0]); });

function handleFile(file) {
    if (file.type !== 'application/pdf') { alert('请上传PDF格式的文件'); return; }
    selectedFile = file;
    analyzeBtn.disabled = false;
    uploadArea.style.borderColor = '#2ecc71';
    uploadArea.querySelector('p').innerHTML = `✅ 已选择：${file.name}`;
}

analyzeBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    analyzeBtn.disabled = true;
    loadingDiv.style.display = 'block';
    resultContainer.style.display = 'none';
    const formData = new FormData();
    formData.append('file', selectedFile);
    try {
        const response = await fetch('/api/analyze', { method: 'POST', body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '分析失败');
        resultDiv.innerText = data.full_report;
        resultContainer.style.display = 'block';
    } catch (err) {
        alert('错误：' + err.message);
    } finally {
        loadingDiv.style.display = 'none';
        analyzeBtn.disabled = false;
    }
});
</script>
</body>
</html>'''
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)