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
        "get_image": "none",
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
    id_match = re.search(r'证件号码[：:]\s*(\d{17}[\dXx])', text)
    if id_match:
        id_num = id_match.group(1)
        try:
            birth_year = int(id_num[6:10])
            birth_month = int(id_num[10:12])
            birth_day = int(id_num[12:14])
            birth_date = datetime(birth_year, birth_month, birth_day)
            age = report_date.year - birth_date.year
            if (report_date.month, report_date.day) < (birth_date.month, birth_date.day):
                age -= 1
            return age
        except:
            pass
    return 0


def extract_marriage(text: str) -> str:
    if "已婚" in text:
        return "已婚"
    elif "未婚" in text:
        return "未婚"
    return "未知"


def extract_report_date(text: str) -> datetime:
    match = re.search(r'报告时间[：:]\s*(\d{4})-(\d{2})-(\d{2})', text)
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

def extract_asset_disposal(text: str) -> Tuple[int, float]:
    count = 0
    balance = 0.0
    # 查找 ## 资产处置信息 下的余额
    asset_section = re.search(r'## 资产处置信息(.*?)(?=## |$)', text, re.DOTALL)
    if asset_section:
        balance_match = re.search(r'余额为([\d,]+)', asset_section.group(1))
        if balance_match:
            count = 1
            balance = clean_number(balance_match.group(1)) / 10000
    return count, balance


def extract_advance_payment(text: str) -> Tuple[int, float]:
    count = 0
    amount = 0.0
    advance_section = re.search(r'## 垫款信息(.*?)(?=## |$)', text, re.DOTALL)
    if advance_section:
        amount_match = re.search(r'累计代偿金额([\d,]+)', advance_section.group(1))
        if amount_match:
            count = 1
            amount = clean_number(amount_match.group(1)) / 10000
    return count, amount

def extract_loans(text: str) -> Dict[str, Any]:
    loans = {
        "count": 0, "balance": 0.0,
        "housing_count": 0, "housing_balance": 0.0,
        "car_count": 0, "car_balance": 0.0,
        "micro_count": 0, "micro_balance": 0.0,
        "overdue_count": 0
    }
    
    # 查找贷款部分
    loan_section = re.search(r'## 贷款(.*?)(?=## |$)', text, re.DOTALL)
    if not loan_section:
        return loans
    
    loan_text = loan_section.group(1)
    
    # 匹配所有以数字加点开头的行
    lines = loan_text.split('\n')
    for line in lines:
        line = line.strip()
        if not line or not re.match(r'^\d+\.', line):
            continue
        
        # 跳过已结清或已转出的账户
        if "已结清" in line or "已转出" in line:
            continue
        
        # 提取余额
        balance_match = re.search(r'余额[为]?([\d,]+)', line)
        if not balance_match:
            continue
        balance = clean_number(balance_match.group(1))
        
        # 提取机构名
        inst_match = re.search(r'\d{4}年\d{1,2}月\d{1,2}日([^发发放授信]+?)(?:发放|为)', line)
        if inst_match:
            institution = inst_match.group(1).strip()
        else:
            institution = line[:50]
        
        # 统计所有未结清账户（余额>0 或 未结清的授信）
        if balance > 0 or ("授信" in line and "余额为0" not in line):
            loans["count"] += 1
            loans["balance"] += balance / 10000
        
        # 判断房贷
        is_housing = any(kw in line for kw in HOUSING_KEYWORDS)
        # 判断车贷
        is_car = any(kw in line for kw in CAR_KEYWORDS)
        # 判断小网贷
        is_micro = is_micro_institution(institution) and not is_housing and not is_car
        
        if is_housing:
            loans["housing_count"] += 1
            loans["housing_balance"] += balance / 10000
        elif is_car:
            loans["car_count"] += 1
            loans["car_balance"] += balance / 10000
        elif is_micro and balance > 0:
            loans["micro_count"] += 1
            loans["micro_balance"] += balance / 10000
        
        if "当前有逾期" in line:
            loans["overdue_count"] += 1
    
    return loans

def extract_credits(text: str) -> Dict[str, Any]:
    credits = {
        "count": 0, "limit": 0.0, "used": 0.0, "overdue": 0,
        "abnormal": {"stop_payment": 0, "frozen": 0, "doubtful": 0}
    }
    
    # 查找信用卡部分
    credit_section = re.search(r'## 信用卡(.*?)(?=## |$)', text, re.DOTALL)
    if not credit_section:
        return credits
    
    credit_text = credit_section.group(1)
    
    lines = credit_text.split('\n')
    for line in lines:
        line = line.strip()
        if not line or not re.match(r'^\d+\.', line):
            continue
        
        # 只统计人民币贷记卡，排除美元账户
        if '贷记卡' not in line or '人民币' not in line:
            continue
        if '美元' in line:
            continue
        # 排除已销户
        if '销户' in line:
            continue
        
        # 提取信用额度
        limit_match = re.search(r'信用额度([\d,]+)', line)
        if not limit_match:
            continue
        limit = clean_number(limit_match.group(1))
        
        # 提取已使用额度
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
        if "呆账" in line:
            credits["abnormal"]["doubtful"] += 1
        if "止付" in line:
            credits["abnormal"]["stop_payment"] += 1
        if "冻结" in line:
            credits["abnormal"]["frozen"] += 1
    
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
    
    # 提取所有逾期月数
    month_pattern = r'最近5年内有(\d+)个月处于逾期状态'
    months = re.findall(month_pattern, text)
    overdue["total_months"] = sum(int(m) for m in months)
    
    # 提取90天以上逾期账户数
    # 匹配 "其中X个月逾期超过90天" 中 X>0 的条数
    overdue_90_pattern = r'其中(\d+)个月逾期超过90天'
    matches = re.findall(overdue_90_pattern, text)
    for match in matches:
        if int(match) > 0:
            overdue["90d_count"] += 1
    
    return overdue

def extract_guarantee(text: str) -> Tuple[int, float]:
    count = 0
    balance = 0.0
    
    guarantee_section = re.search(r'## 相关还款责任信息(.*?)(?=## |$)', text, re.DOTALL)
    if not guarantee_section:
        return count, balance
    
    lines = guarantee_section.group(1).split('\n')
    for line in lines:
        line = line.strip()
        if not line or not re.match(r'^\d+\.', line):
            continue
        
        count += 1
        amount_match = re.search(r'相关还款责任金额([\d,]+)', line)
        if amount_match:
            balance += clean_number(amount_match.group(1)) / 10000
    
    return count, balance

def extract_public_records(text: str) -> str:
    records = []
    
    # 欠税
    tax_match = re.search(r'## 欠税记录.*?欠税总额：([\d,]+)', text, re.DOTALL)
    if tax_match:
        amount = clean_number(tax_match.group(1))
        records.append(f"欠税1条，金额{amount/10000:.2f}万元")
    
    # 民事判决
    judgment_matches = re.findall(r'民事判决记录.*?诉讼标的金额：([\d,]+)', text, re.DOTALL)
    if judgment_matches:
        total = sum(clean_number(j) for j in judgment_matches)
        records.append(f"民事判决{len(judgment_matches)}件，金额{total/10000:.2f}万元")
    
    # 强制执行
    enforcement_matches = re.findall(r'强制执行记录.*?申请执行标的金额：([\d,]+)', text, re.DOTALL)
    if enforcement_matches:
        total = sum(clean_number(e) for e in enforcement_matches)
        records.append(f"强制执行{len(enforcement_matches)}件，金额{total/10000:.2f}万元")
    
    # 行政处罚
    penalty_match = re.search(r'行政处罚记录.*?处罚金额：([\d,]+)', text, re.DOTALL)
    if penalty_match:
        amount = clean_number(penalty_match.group(1))
        records.append(f"行政处罚1条，金额{amount/10000:.2f}万元")
    
    return "\n".join(records) if records else "无"


def extract_queries(text: str, report_date: datetime) -> Dict[str, int]:
    # 暂不实现查询记录统计，返回全0
    return {"30d": 0, "31_90d": 0, "91_180d": 0, "181_360d": 0, "micro_60d": 0, "self_60d": 0}

def build_risk_warning(asset_count: int, asset_balance: float, 
                       advance_count: int, advance_amount: float,
                       loans: Dict, credits: Dict, public_records: str) -> str:
    warnings = []
    if asset_count > 0:
        warnings.append(f"资产处置{asset_count}笔，余额{asset_balance:.2f}万元")
    if advance_count > 0:
        warnings.append(f"垫款{advance_count}笔，金额{advance_amount:.2f}万元")
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
        
        asset_count, asset_balance = extract_asset_disposal(markdown_text)
        advance_count, advance_amount = extract_advance_payment(markdown_text)
        
        risk_warning = build_risk_warning(asset_count, asset_balance, advance_count, advance_amount,
                                          loans, credits, public_records)
        
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
    return {"status": "ok", "version": "v3_full_regex"}


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
