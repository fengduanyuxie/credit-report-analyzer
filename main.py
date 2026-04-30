import os
import json
import base64
import requests
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any

app = FastAPI()

# 允许跨域（如果前端和后端分离）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 从环境变量读取 API Keys（Railway 中配置）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PARSEX_APP_ID = os.environ.get("PARSEX_APP_ID", "0fd9239e2c07003f28d8262745cd3a92")
PARSEX_SECRET = os.environ.get("PARSEX_SECRET", "e87042c286a20aeb61790587432baadd")
PARSEX_API_URL = os.environ.get("PARSEX_API_URL", "https://api.parsex.ai/v1/parse")

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# 系统提示词（完整版）
SYSTEM_PROMPT = """你是一名资深的助贷风控专家，擅长解读征信报告。

请严格按以下要求分析并输出报告：

## 输出要求

请按顺序输出两份报告：

### 第一部分：简要汇总（严格按以下格式，不添加任何额外说明）

*基本信息

性别：{从身份证第17位判断：奇数为男，偶数为女}

年龄：{报告日期年份 - 出生年份，未过生日减1}

婚姻：{已婚/未婚/未知}

{% if 有风险预警 %}风险预警：{如有资产处置、垫款、当逾、非正常、公共记录则汇总显示}{% endif %}

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

{% if 贷款当逾 > 0 %}当逾：{贷款当前逾期账户数}个{% endif %}

机构数：{未结清且有余额的贷款机构数}

总余额：{所有贷款余额总和}万元

{% if 房贷数 > 0 %}房贷数：{房贷笔数（含个人住房、商用房、公积金）}

房贷余额：{房贷余额总和}万元{% endif %}

{% if 车贷数 > 0 %}车贷数：{车贷笔数}

车贷余额：{车贷余额总和}万元{% endif %}

小网贷的机构数：{小网贷机构数（含余额为0的）}

小网贷的余额：{小网贷余额总和}万元

*信用卡

{% if 信用卡当逾 > 0 %}当逾：{信用卡当前逾期账户数}个{% endif %}

{% if 有非正常 %}非正常：{止付/冻结/呆账，格式：X个呆账，X.XX}{% endif %}

机构数：{信用卡机构数（未销户、人民币、额度>0）}

授信额：{信用卡总授信额度}万元

已用额度：{信用卡已用额度}万元

使用率：{使用率}%

{% if 担保户数 > 0 %}

*担保信息

担保户数：{担保户数}

担保余额：{担保余额}万元

{% endif %}

{% if 有公共记录 %}

*公共记录

{如有欠税、民事判决、强制执行、行政处罚，逐行列出}

{% endif %}

### 第二部分：展开分析

请基于以上数据，生成一份专业、详细的征信分析报告，包含以下内容：

1. 基本信息解读（年龄与婚姻分析、风险预警解读）
2. 查询记录分析（频率评估、结构分析、资金紧张程度判断）
3. 逾期记录分析（月数评估、90天以上分析、严重程度判断）
4. 贷款信息分析（负债结构、房贷/车贷分析、小网贷依赖度、当逾解读）
5. 信用卡信息分析（使用率评估、当逾/非正常分析、融资能力判断）
6. 担保信息分析（如有）
7. 公共记录分析（如有）
8. 综合评估与风控建议（风险等级：正常/关注/次级/可疑/损失）

## 提取规则

1. **小网贷判断**：机构名称包含（网商、微众、亿联、金城、裕民、海峡、振兴、新网、苏商、中关村、富民、锡商、百信、长安、兰州、威海、众邦、蓝海、华通、华瑞、友利）或不含"银行"二字
2. **车贷/房贷**：从小网贷中剔除
3. **信用卡**：排除已销户、非人民币账户（美元账户不计入）
4. **查询记录**：排除"贷后管理"
5. **金额单位**：统一转换为万元
6. **年龄**：使用周岁
7. **值为0的项不显示**：包括：公共记录、担保信息、非正常、个呆账、当逾、车贷余额、车贷数、风险预警

## 风险等级判定
- **正常**：无逾期、无不良记录、负债合理
- **关注**：有少量逾期（<3个月）、负债偏高
- **次级**：有30-90天逾期、小网贷使用
- **可疑**：有90天以上逾期、呆账、资产处置
- **损失**：多重严重负面记录、信贷资产无法收回

## 格式要求
- 第一部分不添加任何额外说明文字
- 第二部分语言专业、逻辑清晰、建议具体可行
- 每个判断都要有数据支撑

请分析以下征信报告内容："""


def parse_pdf_with_parsex(pdf_bytes: bytes, filename: str) -> str:
    """使用 ParseX API 解析 PDF"""
    headers = {
        "x-ti-app-id": PARSEX_APP_ID,
        "x-ti-secret-code": PARSEX_SECRET
    }
    
    files = {
        "file": (filename, pdf_bytes, "application/pdf")
    }
    
    try:
        response = requests.post(PARSEX_API_URL, headers=headers, files=files, timeout=60)
        if response.status_code != 200:
            raise Exception(f"ParseX 返回错误: {response.status_code} - {response.text[:200]}")
        
        data = response.json()
        # 根据 ParseX 实际返回格式调整
        extracted_text = data.get("text", "") or data.get("data", {}).get("text", "")
        if not extracted_text:
            # 尝试其他常见字段
            extracted_text = data.get("content", "") or data.get("result", "") or str(data)
        return extracted_text
    except Exception as e:
        raise Exception(f"ParseX 解析失败: {str(e)}")


def call_deepseek(text: str) -> str:
    """调用 DeepSeek API 进行分析"""
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:15000]}  # 截断避免超长
        ],
        "temperature": 0,
        "stream": False
    }
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=120)
    if response.status_code != 200:
        raise Exception(f"DeepSeek API 失败: {response.text[:500]}")
    
    data = response.json()
    return data["choices"][0]["message"]["content"]


@app.post("/api/analyze")
async def analyze_credit_report(file: UploadFile = File(...)):
    # 1. 验证 API Keys
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DeepSeek API Key 未配置，请在 Railway 环境变量中添加 DEEPSEEK_API_KEY")
    
    # 2. 读取上传的 PDF 文件
    try:
        pdf_bytes = await file.read()
        if len(pdf_bytes) > 10 * 1024 * 1024:  # 限制 10MB
            raise HTTPException(status_code=400, detail="文件大小超过 10MB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取文件失败: {str(e)}")
    
    # 3. 调用 ParseX 解析 PDF
    try:
        extracted_text = parse_pdf_with_parsex(pdf_bytes, file.filename)
        if not extracted_text or len(extracted_text) < 50:
            raise Exception("ParseX 未返回有效文本内容")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 解析失败: {str(e)}")
    
    # 4. 调用 DeepSeek 大模型
    try:
        analysis_result = call_deepseek(extracted_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"大模型分析失败: {str(e)}")
    
    return JSONResponse(content={
        "success": True,
        "full_report": analysis_result
    })


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "parsex_configured": bool(PARSEX_APP_ID and PARSEX_SECRET)}


# 提供前端页面
@app.get("/")
async def serve_frontend():
    html_content = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>个人征信报告AI分析系统</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                max-width: 1200px;
                margin: 40px auto;
                padding: 20px;
                background: #f5f7fa;
            }
            .container {
                background: white;
                border-radius: 16px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                padding: 30px;
            }
            h1 {
                color: #1e3c72;
                border-bottom: 2px solid #4a90e2;
                padding-bottom: 10px;
            }
            .upload-area {
                border: 2px dashed #4a90e2;
                border-radius: 12px;
                padding: 40px;
                text-align: center;
                background: #fafcff;
                margin: 20px 0;
                cursor: pointer;
            }
            .upload-area:hover {
                background: #eef4ff;
            }
            input[type="file"] {
                display: none;
            }
            button {
                background: #4a90e2;
                color: white;
                border: none;
                padding: 12px 28px;
                border-radius: 40px;
                font-size: 16px;
                cursor: pointer;
                transition: 0.2s;
            }
            button:hover {
                background: #357abd;
            }
            button:disabled {
                background: #b0c4de;
                cursor: not-allowed;
            }
            .result {
                margin-top: 30px;
                background: #f9f9f9;
                border-radius: 12px;
                padding: 20px;
                white-space: pre-wrap;
                font-family: monospace;
                font-size: 14px;
                line-height: 1.5;
                max-height: 600px;
                overflow-y: auto;
            }
            .loading {
                display: none;
                text-align: center;
                margin: 20px;
                color: #4a90e2;
            }
            .error {
                color: #d32f2f;
                background: #ffebee;
                padding: 12px;
                border-radius: 8px;
                margin: 20px 0;
            }
            .tab-buttons {
                display: flex;
                gap: 10px;
                margin-bottom: 15px;
            }
            .tab-btn {
                background: #e0e7ff;
                color: #1e3c72;
                padding: 8px 16px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
            }
            .tab-btn.active {
                background: #4a90e2;
                color: white;
            }
        </style>
    </head>
    <body>
    <div class="container">
        <h1>📄 个人征信报告AI分析系统</h1>
        <p>上传PDF格式的个人信用报告，系统将自动解析并生成专业风控报告。</p>
    
        <div class="upload-area" id="uploadArea">
            <p>📎 点击或拖拽上传PDF文件</p>
            <input type="file" id="fileInput" accept=".pdf">
        </div>
    
        <div style="text-align: center;">
            <button id="analyzeBtn" disabled>开始分析</button>
        </div>
    
        <div class="loading" id="loading">
            ⏳ 正在解析并分析报告，请稍候...（可能需要30-60秒）
        </div>
    
        <div id="resultContainer" style="display: none;">
            <div class="tab-buttons">
                <button class="tab-btn active" data-tab="summary">📋 简要汇总</button>
                <button class="tab-btn" data-tab="detail">🔍 展开分析</button>
            </div>
            <div class="result" id="summaryResult"></div>
            <div class="result" id="detailResult" style="display: none;"></div>
        </div>
    </div>
    
    <script>
        let selectedFile = null;
        const fileInput = document.getElementById('fileInput');
        const uploadArea = document.getElementById('uploadArea');
        const analyzeBtn = document.getElementById('analyzeBtn');
        const loadingDiv = document.getElementById('loading');
        const resultContainer = document.getElementById('resultContainer');
        const summaryDiv = document.getElementById('summaryResult');
        const detailDiv = document.getElementById('detailResult');
    
        uploadArea.addEventListener('click', () => fileInput.click());
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.style.background = '#eef4ff';
        });
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.style.background = '#fafcff';
        });
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.style.background = '#fafcff';
            const files = e.dataTransfer.files;
            if (files.length > 0) handleFile(files[0]);
        });
    
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) handleFile(e.target.files[0]);
        });
    
        function handleFile(file) {
            if (file.type !== 'application/pdf') {
                alert('请上传PDF格式的文件');
                return;
            }
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
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    body: formData
                });
    
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || '分析失败');
    
                const full = data.full_report;
                let summary = full;
                let detail = full;
                if (full.includes('### 第一部分') && full.includes('### 第二部分')) {
                    const parts = full.split('### 第二部分');
                    summary = parts[0].replace('### 第一部分', '').trim();
                    detail = '### 第二部分' + (parts[1] || '').trim();
                }
                summaryDiv.innerText = summary;
                detailDiv.innerText = detail;
    
                resultContainer.style.display = 'block';
                document.querySelector('[data-tab="summary"]').click();
    
            } catch (err) {
                alert('错误：' + err.message);
            } finally {
                loadingDiv.style.display = 'none';
                analyzeBtn.disabled = false;
            }
        });
    
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                const tab = btn.getAttribute('data-tab');
                if (tab === 'summary') {
                    summaryDiv.style.display = 'block';
                    detailDiv.style.display = 'none';
                } else {
                    summaryDiv.style.display = 'none';
                    detailDiv.style.display = 'block';
                }
            });
        });
    </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)