import os
import re
import json
import requests
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import Dict, Any, List, Tuple

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== 配置 ==========
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

TEXTIN_APP_ID = "0fd9239e2c07003f28d8262745cd3a92"
TEXTIN_SECRET_CODE = "e87042c286a20aeb61790587432baadd"
TEXTIN_API_URL = "https://api.textin.com/ai/service/v1/pdf_to_markdown"

MICRO_KEYWORDS = [
    "网商", "微众", "亿联", "金城", "裕民", "海峡", "振兴", "新网",
    "苏商", "中关村", "富民", "锡商", "百信", "长安", "兰州",
    "威海", "众邦", "蓝海", "华通", "华瑞", "友利"
]

HOUSING_KEYWORDS = ["个人住房", "住房贷款", "商用房", "公积金"]
CAR_KEYWORDS = ["汽车"]

QUERY_REASONS_TO_COUNT = ["贷款审批", "信用卡审批", "保前审查", "担保资格审查", "法人代表、负责人、高管等资信审查"]
EXCLUDED_QUERY_REASONS = ["贷后管理"]


def clean_number(num_str: str) -> float:
    if not num_str:
        return 0.0
    cleaned = num_str.replace(' ', '').replace('，', '').replace(',', '')
    try:
        return float(cleaned)
    except:
        return 0.0


def parse_pdf_with_textin(pdf_bytes: bytes) -> str:
    headers = {
        "x-ti-app-id": TEXTIN_APP_ID,
        "x-ti-secret-code": TEXTIN_SECRET_CODE,
        "Content-Type": "application/octet-stream"
    }
    params = {
        "dpi": 144,
        "get_image": "objects",
        "markdown_details": 1,
        "page_count": 100,
        "parse_mode": "scan",
        "table_flavor": "html"
    }
    response = requests.post(TEXTIN_API_URL, params=params, headers=headers, data=pdf_bytes, timeout=60)
    if response.status_code != 200:
        raise Exception(f"TextIn API HTTP错误: {response.status_code}")
    result = response.json()
    if result.get("code") != 200:
        raise Exception(f"TextIn 业务错误: {result.get('message', '未知错误')}")
    markdown = result.get("result", {}).get("markdown", "")
    if not markdown:
        raise Exception("TextIn 未返回文本内容")
    return markdown


def extract_gender(text: str) -> str:
    match = re.search(r'证件号码[：:]\s*(\d{17}[\dXx])', text)
    if match:
        id_num = match.group(1)
        gender_code = int(id_num[16])
        return "男" if gender_code % 2 == 1 else "女"
    return "未知"


def extract_age(text: str, report_date: datetime) -> int:
    birth_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if birth_match:
        birth_year = int(birth_match.group(1))
        birth_month = int(birth_match.group(2))
        birth_day = int(birth_match.group(3))
        birth_date = datetime(birth_year, birth_month, birth_day)
        age = report_date.year - birth_date.year
        if (report_date.month, report_date.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    return 0


def extract_marriage(text: str) -> str:
    if "已婚" in text:
        return "已婚"
    elif "未婚" in text:
        return "未婚"
    return "未知"


def extract_report_date(text: str) -> datetime:
    match = re.search(r'报告时间[：:]\s*(\d{4})[-年]\s*(\d{1,2})[-月]\s*(\d{1,2})', text)
    if match:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return datetime.now()


def is_micro_institution(institution_name: str) -> bool:
    for kw in MICRO_KEYWORDS:
        if kw in institution_name:
            return True
    if "银行" not in institution_name:
        return True
    return False


def extract_queries(text: str, report_date: datetime) -> Dict[str, int]:
    queries = {"30d": 0, "31_90d": 0, "91_180d": 0, "181_360d": 0, "micro_60d": 0, "self_60d": 0}
    pattern = r'(\d{4})年(\d{1,2})月(\d{1,2})日\s+([^\d\n]+?)\s+(贷款审批|信用卡审批|贷后管理|保前审查|担保资格审查|法人代表、负责人、高管等资信审查|本人查询)'
    matches = re.findall(pattern, text)
    for y, m, d, institution, reason in matches:
        query_date = datetime(int(y), int(m), int(d))
        diff_days = (report_date - query_date).days
        if "本人查询" in reason:
            if diff_days <= 60:
                queries["self_60d"] += 1
            continue
        if reason in EXCLUDED_QUERY_REASONS:
            continue
        if reason not in QUERY_REASONS_TO_COUNT:
            continue
        if diff_days <= 30:
            queries["30d"] += 1
            if diff_days <= 60 and is_micro_institution(institution):
                queries["micro_60d"] += 1
        elif 31 <= diff_days <= 90:
            queries["31_90d"] += 1
            if diff_days <= 60 and is_micro_institution(institution):
                queries["micro_60d"] += 1
        elif 91 <= diff_days <= 180:
            queries["91_180d"] += 1
        elif 181 <= diff_days <= 360:
            queries["181_360d"] += 1
    return queries


def extract_loans(text: str) -> Dict[str, Any]:
    loans = {
        "count": 0, "balance": 0.0,
        "housing_count": 0, "housing_balance": 0.0,
        "car_count": 0, "car_balance": 0.0,
        "micro_count": 0, "micro_balance": 0.0,
        "overdue_count": 0
    }
    
    lines = text.split('\n')
    for line in lines:
        # 匹配贷款行：以数字加点开头，包含"发放"和"余额"
        if re.match(r'^\d+\.', line) and '发放' in line and '余额' in line:
            # 提取余额（格式：余额429,167 或 余额429,167。）
            balance_match = re.search(r'余额([\d,]+)', line)
            if not balance_match:
                continue
            balance = clean_number(balance_match.group(1))
            
            # 提取机构名（从日期后到"发放"之前）
            inst_match = re.search(r'\d{4}年\d{1,2}月\d{1,2}日([^发]+)发放', line)
            if inst_match:
                institution = inst_match.group(1).strip()
            else:
                institution = line
            
            desc = line
            
            # 只统计余额 > 0 的账户
            if balance > 0:
                loans["count"] += 1
                loans["balance"] += balance / 10000
            
            is_housing = any(kw in desc for kw in HOUSING_KEYWORDS)
            is_car = any(kw in desc for kw in CAR_KEYWORDS)
            is_micro = is_micro_institution(institution) and not is_housing and not is_car
            
            if is_housing:
                loans["housing_count"] += 1
                loans["housing_balance"] += balance / 10000
            elif is_car:
                loans["car_count"] += 1
                loans["car_balance"] += balance / 10000
            elif is_micro:
                loans["micro_count"] += 1
                loans["micro_balance"] += balance / 10000
        
        # 匹配授信类账户（格式：余额为5,091）
        elif re.match(r'^\d+\.', line) and '授信' in line and '余额为' in line:
            balance_match = re.search(r'余额为([\d,]+)', line)
            if not balance_match:
                continue
            balance = clean_number(balance_match.group(1))
            
            # 提取机构名
            inst_match = re.search(r'\d{4}年\d{1,2}月\d{1,2}日([^为]+)为', line)
            if inst_match:
                institution = inst_match.group(1).strip()
            else:
                institution = line
            
            desc = line
            
            # 授信类账户计入机构数（余额=0也计入）
            loans["count"] += 1
            loans["balance"] += balance / 10000
            
            is_housing = any(kw in desc for kw in HOUSING_KEYWORDS)
            is_car = any(kw in desc for kw in CAR_KEYWORDS)
            is_micro = is_micro_institution(institution) and not is_housing and not is_car
            
            if is_housing:
                loans["housing_count"] += 1
                loans["housing_balance"] += balance / 10000
            elif is_car:
                loans["car_count"] += 1
                loans["car_balance"] += balance / 10000
            elif is_micro:
                loans["micro_count"] += 1
                loans["micro_balance"] += balance / 10000
        
        # 匹配当前逾期
        if "当前有逾期" in line:
            loans["overdue_count"] += 1
    
    return loans


def extract_credits(text: str) -> Dict[str, Any]:
    credits = {
        "count": 0, "limit": 0.0, "used": 0.0, "overdue": 0,
        "abnormal": {"stop_payment": 0, "frozen": 0, "doubtful": 0}
    }
    
    lines = text.split('\n')
    for line in lines:
        if re.match(r'^\d+\.', line) and '贷记卡' in line and '人民币账户' in line:
            # 提取信用额度
            limit_match = re.search(r'信用额度([\d,]+)', line)
            if not limit_match:
                continue
            limit = clean_number(limit_match.group(1))
            
            used_match = re.search(r'已使用额度([\d,]+)', line)
            if not used_match:
                used_match = re.search(r'余额([\d,]+)', line)
            used = clean_number(used_match.group(1)) if used_match else 0
            
            if limit > 0:
                credits["count"] += 1
                credits["limit"] += limit / 10000
                credits["used"] += used / 10000
            
            if "当前有逾期" in line:
                credits["overdue"] += 1
            if "止付" in line:
                credits["abnormal"]["stop_payment"] += 1
            if "冻结" in line:
                credits["abnormal"]["frozen"] += 1
            if "呆账" in line:
                credits["abnormal"]["doubtful"] += 1
    
    credits["usage_rate"] = round((credits["used"] / credits["limit"] * 100)) if credits["limit"] > 0 else 0
    
    abnormal_parts = []
    if credits["abnormal"]["stop_payment"] > 0:
        abnormal_parts.append(f"止付{credits['abnormal']['stop_payment']}个")
    if credits["abnormal"]["frozen"] > 0:
        abnormal_parts.append(f"冻结{credits['abnormal']['frozen']}个")
    if credits["abnormal"]["doubtful"] > 0:
        abnormal_parts.append(f"呆账{credits['abnormal']['doubtful']}个")
    credits["abnormal_display"] = "；".join(abnormal_parts) if abnormal_parts else ""
    
    return credits


def extract_overdue(text: str) -> Dict[str, int]:
    overdue = {"total_months": 0, "90d_count": 0}
    month_pattern = r'最近5年内有(\d+)个月处于逾期状态'
    months = re.findall(month_pattern, text)
    overdue["total_months"] = sum(int(m) for m in months)
    overdue_90_pattern = r'发生过90天以上逾期'
    overdue["90d_count"] = len(re.findall(overdue_90_pattern, text))
    return overdue


def extract_guarantee(text: str) -> Tuple[int, float]:
    count = 0
    balance = 0.0
    pattern = r'(\d+)\.\s*(\d{4})年\d{1,2}月\d{1,2}日，为[^，]+?[公司个人][^，]*?相关还款责任金额([\d,，]+)'
    matches = re.findall(pattern, text)
    for match in matches:
        count += 1
        balance += clean_number(match[1]) / 10000
    return count, balance


def extract_public_records(text: str) -> str:
    records = []
    tax_match = re.search(r'欠税记录.*?欠税总额：([\d,，]+)', text, re.DOTALL)
    if tax_match:
        amount = clean_number(tax_match.group(1))
        records.append(f"欠税1条，金额{amount/10000:.2f}万元")
    judgment_matches = re.findall(r'民事判决记录.*?诉讼标的金额：([\d,，]+)', text, re.DOTALL)
    if judgment_matches:
        total = sum(clean_number(j) for j in judgment_matches)
        records.append(f"民事判决{len(judgment_matches)}件，金额{total/10000:.2f}万元")
    enforcement_matches = re.findall(r'强制执行记录.*?申请执行标的金额：([\d,，]+)', text, re.DOTALL)
    if enforcement_matches:
        total = sum(clean_number(e) for e in enforcement_matches)
        records.append(f"强制执行{len(enforcement_matches)}件，金额{total/10000:.2f}万元")
    penalty_match = re.search(r'行政处罚记录.*?处罚金额：([\d,，]+)', text, re.DOTALL)
    if penalty_match:
        amount = clean_number(penalty_match.group(1))
        records.append(f"行政处罚1条，金额{amount/10000:.2f}万元")
    return "\n".join(records) if records else "无"


def build_risk_warning(loans: Dict, credits: Dict, public_records: str) -> str:
    warnings = []
    if loans.get("overdue_count", 0) > 0:
        warnings.append(f"贷款当逾{loans['overdue_count']}个")
    if credits.get("overdue", 0) > 0:
        warnings.append(f"信用卡当逾{credits['overdue']}个")
    if credits.get("abnormal_display"):
        warnings.append(credits["abnormal_display"])
    if public_records != "无":
        warnings.append(public_records.replace("\n", "；"))
    return "；".join(warnings) if warnings else "无"


def build_llm_prompt(stats: Dict[str, Any]) -> str:
    q = stats["queries"]
    l = stats["loans"]
    c = stats["credits"]
    o = stats["overdue"]
    return f"""你是一名资深的助贷风控专家。

请基于以下【真实统计数据】，生成一份专业的征信分析报告（仅需第二部分：展开分析），不要简单重复第一部分的数字。

### 基础信息
- 性别：{stats['gender']}，年龄：{stats['age']}，婚姻：{stats['marriage']}

### 查询记录分析数据
- 30天内：{q['30d']}次
- 31-90天：{q['31_90d']}次
- 91-180天：{q['91_180d']}次
- 181-360天：{q['181_360d']}次
- 60天内小网贷查询：{q['micro_60d']}次
- 60天内本人查询：{q['self_60d']}次

### 贷款数据分析
- 总机构数：{l['count']}家
- 总余额：{round(l['balance'], 2)}万元
- 房贷：{l['housing_count']}笔，余额：{round(l['housing_balance'], 2)}万元
- 车贷：{l['car_count']}笔，余额：{round(l['car_balance'], 2)}万元
- 小网贷：{l['micro_count']}家，余额：{round(l['micro_balance'], 2)}万元
- 当前逾期：{l['overdue_count']}个

### 信用卡数据分析
- 机构数：{c['count']}家
- 授信额：{round(c['limit'], 2)}万元
- 已用额度：{round(c['used'], 2)}万元
- 使用率：{c['usage_rate']}%
- 当前逾期：{c['overdue']}个

### 逾期记录
- 总逾期月数：{o['total_months']}个月
- 90天以上账户：{o['90d_count']}个

请按以下结构输出：
1. 基本信息解读
2. 查询记录分析
3. 逾期记录分析
4. 贷款信息分析
5. 信用卡信息分析
6. 综合评估与风控建议

要求：语言专业、逻辑清晰、每个判断都要有数据支撑。"""


def call_deepseek(prompt: str) -> str:
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=120)
    if response.status_code != 200:
        raise Exception(f"DeepSeek API 错误: {response.status_code} - {response.text[:200]}")
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
        markdown_text = parse_pdf_with_textin(pdf_bytes)
        print("=== TextIn 完整解析结果（前5000字符）===")
        print(markdown_text[:5000])
        print("=========================================")
        
        report_date = extract_report_date(markdown_text)
        gender = extract_gender(markdown_text)
        age = extract_age(markdown_text, report_date)
        marriage = extract_marriage(markdown_text)
        queries = extract_queries(markdown_text, report_date)
        loans = extract_loans(markdown_text)
        credits = extract_credits(markdown_text)
        overdue = extract_overdue(markdown_text)
        guarantee_count, guarantee_balance = extract_guarantee(markdown_text)
        public_records = extract_public_records(markdown_text)
        risk_warning = build_risk_warning(loans, credits, public_records)
        
        stats = {
            "gender": gender, "age": age, "marriage": marriage,
            "queries": queries, "loans": loans, "credits": credits, "overdue": overdue
        }
        
        part1 = f"""### 第一部分：简要汇总

*基本信息
性别：{gender}
年龄：{age}
婚姻：{marriage}
风险预警：{risk_warning}

*查询记录
30天内：{queries['30d']}
31-90天：{queries['31_90d']}
90-180天：{queries['91_180d']}
180-360天：{queries['181_360d']}
60天内小网贷：{queries['micro_60d']}
60天内本人：{queries['self_60d']}

*5年内逾期
总月数：{overdue['total_months']}
90天以上的账户数：{overdue['90d_count']}

*贷款
机构数：{loans['count']}
总余额：{round(loans['balance'], 2)}万元
房贷数：{loans['housing_count']}
房贷余额：{round(loans['housing_balance'], 2)}万元
车贷数：{loans['car_count']}
车贷余额：{round(loans['car_balance'], 2)}万元
小网贷的机构数：{loans['micro_count']}
小网贷的余额：{round(loans['micro_balance'], 2)}万元

*信用卡
机构数：{credits['count']}
授信额：{round(credits['limit'], 2)}万元
已用额度：{round(credits['used'], 2)}万元
使用率：{credits['usage_rate']}%

*担保信息
担保户数：{guarantee_count}
担保余额：{round(guarantee_balance, 2)}万元

*公共记录
{public_records}"""
        
        llm_prompt = build_llm_prompt(stats)
        part2 = call_deepseek(llm_prompt)
        
        full_report = part1 + "\n\n### 第二部分：展开分析\n\n" + part2
        return JSONResponse({"success": True, "full_report": full_report})
        
    except Exception as e:
        raise HTTPException(500, f"处理失败: {str(e)}")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "v3_final_fixed"}


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
.result {background: #f9f9f9; border-radius: 12px; padding: 20px; margin-top: 20px; white-space: pre-wrap; font-family: monospace; font-size: 14px; line-height: 1.5; max-height: 600px; overflow-y: auto;}
.loading {display: none; text-align: center; margin: 20px; color: #4a90e2;}
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
const resultDiv = document.getElementById('result');
const resultContainer = document.getElementById('resultContainer');

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