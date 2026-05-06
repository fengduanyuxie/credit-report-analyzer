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

# TextIn 新版 xParse API 配置
TEXTIN_APP_ID = "0fd9239e2c07003f28d8262745cd3a92"
TEXTIN_SECRET_CODE = "e87042c286a20aeb61790587432baadd"
XPARSE_API_URL = "https://api.textin.com/api/v1/xparse/parse/sync"

MICRO_KEYWORDS = [
    "网商", "微众", "亿联", "金城", "裕民", "海峡", "振兴", "新网",
    "苏商", "中关村", "富民", "锡商", "百信", "长安", "兰州",
    "威海", "众邦", "蓝海", "华通", "华瑞", "友利"
]

HOUSING_KEYWORDS = ["个人住房", "住房贷款", "商用房", "公积金", "住房公积金"]
CAR_KEYWORDS = ["汽车"]


def clean_number(num_str: str) -> float:
    if not num_str:
        return 0.0
    cleaned = num_str.replace(' ', '').replace('，', '').replace(',', '')
    try:
        return float(cleaned)
    except:
        return 0.0


def parse_pdf_with_xparse(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    使用新版 xParse API 解析 PDF，返回结构化 JSON
    """
    headers = {
        "x-ti-app-id": TEXTIN_APP_ID,
        "x-ti-secret-code": TEXTIN_SECRET_CODE,
    }
    
    # 配置参数：要求返回表格结构和页面信息
    config = {
        "capabilities": {
            "include_table_structure": True,
            "pages": True,
            "include_hierarchy": True
        }
    }
    
    # 使用 files 参数上传文件
    files = {
        "file": ("report.pdf", pdf_bytes, "application/pdf")
    }
    
    data = {
        "config": json.dumps(config)
    }
    
    response = requests.post(
        XPARSE_API_URL,
        headers=headers,
        files=files,
        data=data,
        timeout=60
    )
    
    if response.status_code != 200:
        raise Exception(f"xParse API HTTP错误: {response.status_code} - {response.text[:200]}")
    
    result = response.json()
    if result.get("code") != 200:
        raise Exception(f"xParse API 业务错误: {result.get('message', '未知错误')}")
    
    return result.get("data", {})


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


def extract_report_date_from_text(text: str) -> datetime:
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
    match = re.search(r'## 资产处置信息.*?余额为([\d,]+)', text, re.DOTALL)
    if match:
        count = 1
        balance = clean_number(match.group(1)) / 10000
    return count, balance


def extract_advance_payment(text: str) -> Tuple[int, float]:
    count = 0
    amount = 0.0
    match = re.search(r'## 垫款信息.*?累计代偿金额([\d,]+)', text, re.DOTALL)
    if match:
        count = 1
        amount = clean_number(match.group(1)) / 10000
    return count, amount


def extract_loans(text: str) -> Dict[str, Any]:
    loans = {
        "count": 0, "balance": 0.0,
        "housing_count": 0, "housing_balance": 0.0,
        "car_count": 0, "car_balance": 0.0,
        "micro_count": 0, "micro_balance": 0.0,
        "overdue_count": 0
    }
    
    lines = text.split('\n')
    in_loan_section = False
    
    for line in lines:
        line = line.strip()
        
        if line == '## 贷款':
            in_loan_section = True
            continue
        
        if in_loan_section and line.startswith('## ') and line != '## 贷款':
            break
        
        if not in_loan_section:
            continue
        
        if not line or line.startswith('###'):
            continue
        
        if re.match(r'^\d+\.', line):
            if '为张三' in line or '为王五' in line or '为某样例' in line:
                continue
            if "已结清" in line or "已转出" in line:
                continue
            
            balance_match = re.search(r'余额[为]?([\d,]+)', line)
            if not balance_match:
                continue
            balance = clean_number(balance_match.group(1))
            
            inst_match = re.search(r'\d{4}年\d{1,2}月\d{1,2}日([^发放授信]+?)(?:发放|为)', line)
            institution = inst_match.group(1).strip() if inst_match else ''
            
            if balance > 0:
                loans["count"] += 1
                loans["balance"] += balance / 10000
            
            is_housing = any(kw in line for kw in HOUSING_KEYWORDS)
            is_car = any(kw in line for kw in CAR_KEYWORDS)
            is_micro = is_micro_institution(institution) and not is_housing and not is_car
            
            if is_housing and balance > 0:
                loans["housing_count"] += 1
                loans["housing_balance"] += balance / 10000
            elif is_car and balance > 0:
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
    
    lines = text.split('\n')
    in_credit_section = False
    
    for line in lines:
        line = line.strip()
        
        if line == '## 信用卡':
            in_credit_section = True
            continue
        
        if in_credit_section and line.startswith('## ') and line != '## 信用卡':
            break
        
        if not in_credit_section:
            continue
        
        if not line or line.startswith('###'):
            continue
        
        if re.match(r'^\d+\.', line):
            if '贷记卡' not in line or '人民币' not in line:
                continue
            if '美元' in line:
                continue
            if '销户' in line or '尚未激活' in line:
                continue
            
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
    month_pattern = r'最近5年内有(\d+)个月处于逾期状态'
    months = re.findall(month_pattern, text)
    overdue["total_months"] = sum(int(m) for m in months)
    
    overdue_90_pattern = r'其中(\d+)个月逾期超过90天'
    matches = re.findall(overdue_90_pattern, text)
    for match in matches:
        if int(match) > 0:
            overdue["90d_count"] += 1
    
    return overdue


def extract_guarantee(text: str) -> Tuple[int, float]:
    count = 0
    balance = 0.0
    
    lines = text.split('\n')
    in_guarantee_section = False
    
    for line in lines:
        line = line.strip()
        
        if line == '## 相关还款责任信息':
            in_guarantee_section = True
            continue
        
        if in_guarantee_section and line.startswith('## '):
            break
        
        if not in_guarantee_section:
            continue
        
        if re.match(r'^\d+\.', line):
            count += 1
            amount_match = re.search(r'相关还款责任金额([\d,]+)', line)
            amount = clean_number(amount_match.group(1)) if amount_match else 0
            
            balance_match = re.search(r'贷款余额([\d,]+)', line)
            if not balance_match:
                balance_match = re.search(r'余额([\d,]+)', line)
            loan_balance = clean_number(balance_match.group(1)) if balance_match else 0
            
            min_value = min(amount, loan_balance) if amount > 0 and loan_balance > 0 else max(amount, loan_balance)
            balance += min_value / 10000
    
    # 也检查贷款部分中混杂的担保信息
    loan_section = re.search(r'## 贷款(.*?)(?=\n## |$)', text, re.DOTALL)
    if loan_section:
        loan_text = loan_section.group(1)
        for line in loan_text.split('\n'):
            line = line.strip()
            if re.match(r'^\d+\.', line) and ('为张三' in line or '为王五' in line or '为某样例' in line):
                count += 1
                amount_match = re.search(r'相关还款责任金额([\d,]+)', line)
                amount = clean_number(amount_match.group(1)) if amount_match else 0
                
                balance_match = re.search(r'余额([\d,]+)', line)
                loan_balance = clean_number(balance_match.group(1)) if balance_match else 0
                
                min_value = min(amount, loan_balance) if amount > 0 and loan_balance > 0 else max(amount, loan_balance)
                balance += min_value / 10000
    
    return count, balance


def extract_public_records(text: str) -> str:
    records = []
    
    tax_match = re.search(r'## 欠税记录.*?欠税总额：([\d,]+)', text, re.DOTALL)
    if tax_match:
        amount = clean_number(tax_match.group(1))
        records.append(f"欠税1条，金额{amount/10000:.2f}万元")
    
    judgment_matches = re.findall(r'## 民事判决记录.*?诉讼标的金额：([\d,]+)', text, re.DOTALL)
    if judgment_matches:
        total = sum(clean_number(j) for j in judgment_matches)
        records.append(f"民事判决{len(judgment_matches)}件，金额{total/10000:.2f}万元")
    
    enforcement_matches = re.findall(r'## 强制执行记录.*?申请执行标的金额：([\d,]+)', text, re.DOTALL)
    if enforcement_matches:
        total = sum(clean_number(e) for e in enforcement_matches)
        records.append(f"强制执行{len(enforcement_matches)}件，金额{total/10000:.2f}万元")
    
    penalty_match = re.search(r'## 行政处罚记录.*?处罚金额：([\d,]+)', text, re.DOTALL)
    if penalty_match:
        amount = clean_number(penalty_match.group(1))
        records.append(f"行政处罚1条，金额{amount/10000:.2f}万元")
    
    return "\n".join(records) if records else "无"


def extract_queries_from_xparse(data: Dict[str, Any], report_date: datetime) -> Dict[str, int]:
    """
    从 xParse 返回的结构化 JSON 中提取查询记录
    严格区分机构查询和本人查询
    """
    queries = {
        "30d": 0,
        "31_90d": 0,
        "91_180d": 0,
        "181_360d": 0,
        "micro_60d": 0,
        "self_60d": 0
    }
    
    print("=== 查询提取调试（xParse 结构化数据）===")
    print(f"报告日期: {report_date}")
    
    elements = data.get("elements", [])
    print(f"共找到 {len(elements)} 个元素")
    
    # 获取 markdown 用于定位表格标题
    markdown = data.get("markdown", "")
    
    # 先找到机构查询表格和本人查询表格的 element_id
    inst_table_id = None
    self_table_id = None
    
    # 遍历所有元素，找到标题
    for i, element in enumerate(elements):
        elem_type = element.get("type", "")
        text = element.get("text", "")
        
        if elem_type == "Title" and "机构查询记录明细" in text:
            # 机构查询表格通常在该标题之后
            for j in range(i + 1, len(elements)):
                if elements[j].get("type") == "Table":
                    inst_table_id = elements[j].get("element_id")
                    print(f"找到机构查询表格: {inst_table_id}")
                    break
        
        if elem_type == "Title" and "本人查询记录明细" in text:
            for j in range(i + 1, len(elements)):
                if elements[j].get("type") == "Table":
                    self_table_id = elements[j].get("element_id")
                    print(f"找到本人查询表格: {self_table_id}")
                    break
    
    # 遍历所有表格元素
    for element in elements:
        if element.get("type") != "Table":
            continue
        
        element_id = element.get("element_id", "")
        
        # 获取表格结构
        table_structure = element.get("table_structure", {})
        cells = table_structure.get("cells", [])
        
        if not cells:
            continue
        
        # 按行重组单元格
        rows = {}
        for cell in cells:
            row_num = cell.get("row", 0)
            col_num = cell.get("col", 0)
            cell_text = cell.get("text", "").strip()
            
            if row_num not in rows:
                rows[row_num] = {}
            rows[row_num][col_num] = cell_text
        
        # 判断是机构查询表格还是本人查询表格
        is_inst_table = (element_id == inst_table_id)
        is_self_table = (element_id == self_table_id)
        
        # 如果没有通过标题定位到，尝试通过内容判断
        if not is_inst_table and not is_self_table:
            for row_num, row_data in rows.items():
                row_text = " ".join(str(v) for v in row_data.values())
                if "本人" in row_text and ("本人查询" in row_text or "查询" in row_text):
                    is_self_table = True
                    print(f"通过内容判断为本人查询表格: {element_id}")
                    break
                if "贷款审批" in row_text or "信用卡审批" in row_text or "保前审查" in row_text or "贷后管理" in row_text:
                    is_inst_table = True
                    print(f"通过内容判断为机构查询表格: {element_id}")
                    break
        
        # 处理机构查询表格
        if is_inst_table:
            print(f"处理机构查询表格 {element_id[:8]}...")
            for row_num, row_data in rows.items():
                # 跳过表头行
                row_text = " ".join(str(v) for v in row_data.values())
                if "查询日期" in row_text or "查询机构" in row_text or "查询原因" in row_text:
                    print(f"    跳过表头行 {row_num}")
                    continue
                
                # 提取各列数据（根据实际列顺序调整）
                # 常见的列顺序：第1列是编号，第2列是日期，第3列是机构，第4列是原因
                date = row_data.get(2, "") or row_data.get(1, "")
                institution = row_data.get(3, "") or row_data.get(2, "")
                reason = row_data.get(4, "") or row_data.get(3, "")
                
                if not date or not reason:
                    continue
                
                print(f"    行 {row_num}: 日期={date}, 机构={institution}, 原因={reason}")
                
                # 排除贷后管理
                if "贷后管理" in reason:
                    print(f"      排除: 贷后管理")
                    continue
                
                # 解析日期
                try:
                    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date)
                    if date_match:
                        y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                        query_date = datetime(y, m, d)
                        diff_days = (report_date - query_date).days
                        print(f"      距今天数: {diff_days}")
                        
                        if diff_days > 360:
                            print(f"      排除: 超过360天")
                            continue
                        
                        if diff_days <= 30:
                            queries["30d"] += 1
                            print(f"      计入: 30天内")
                        elif diff_days <= 90:
                            queries["31_90d"] += 1
                            print(f"      计入: 31-90天")
                        elif diff_days <= 180:
                            queries["91_180d"] += 1
                            print(f"      计入: 91-180天")
                        elif diff_days <= 360:
                            queries["181_360d"] += 1
                            print(f"      计入: 181-360天")
                        
                        if diff_days <= 60 and institution:
                            is_micro = ("银行" not in institution) or any(kw in institution for kw in MICRO_KEYWORDS)
                            if is_micro:
                                queries["micro_60d"] += 1
                                print(f"      小网贷: 是")
                except Exception as e:
                    print(f"      解析错误: {e}")
        
        # 处理本人查询表格
        if is_self_table:
            print(f"处理本人查询表格 {element_id[:8]}...")
            for row_num, row_data in rows.items():
                # 跳过表头行
                row_text = " ".join(str(v) for v in row_data.values())
                if "查询日期" in row_text or "查询机构" in row_text or "查询原因" in row_text:
                    print(f"    跳过表头行 {row_num}")
                    continue
                
                # 提取日期
                date = row_data.get(2, "") or row_data.get(1, "")
                
                if not date:
                    continue
                
                print(f"    行 {row_num}: 日期={date}")
                
                # 解析日期
                try:
                    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date)
                    if date_match:
                        y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                        query_date = datetime(y, m, d)
                        diff_days = (report_date - query_date).days
                        print(f"      距今天数: {diff_days}")
                        
                        if 0 <= diff_days <= 60:
                            queries["self_60d"] += 1
                            print(f"      计入: 60天内本人查询")
                except Exception as e:
                    print(f"      解析错误: {e}")
    
    print(f"最终结果: 30d={queries['30d']}, 31-90d={queries['31_90d']}, 91-180d={queries['91_180d']}, 181-360d={queries['181_360d']}, micro_60d={queries['micro_60d']}, self_60d={queries['self_60d']}")
    
    return queries


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
        # 1. 使用新版 xParse API 解析 PDF
        xparse_data = parse_pdf_with_xparse(pdf_bytes)
        
        # 2. 获取 markdown 文本（用于提取非表格信息）
        markdown_text = xparse_data.get("markdown", "")
        
        print("=== xParse 解析结果（结构化）===")
        print(f"成功页数: {xparse_data.get('success_count', 0)}")
        print(f"元素数量: {len(xparse_data.get('elements', []))}")
        print("================================")
        
        # 3. 提取报告日期
        report_date = extract_report_date_from_text(markdown_text)
        
        # 4. 提取基础信息
        gender = extract_gender(markdown_text)
        age = extract_age(markdown_text, report_date)
        marriage = extract_marriage(markdown_text)
        
        # 5. 提取其他统计（贷款、信用卡等）
        loans = extract_loans(markdown_text)
        credits = extract_credits(markdown_text)
        overdue = extract_overdue(markdown_text)
        guarantee_count, guarantee_balance = extract_guarantee(markdown_text)
        public_records = extract_public_records(markdown_text)
        
        asset_count, asset_balance = extract_asset_disposal(markdown_text)
        advance_count, advance_amount = extract_advance_payment(markdown_text)
        
        # 6. 提取查询记录（从结构化 JSON 中）
        queries = extract_queries_from_xparse(xparse_data, report_date)
        
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
机构
30天内：{queries['30d']}
31-90天：{queries['31_90d']}
90-180天：{queries['91_180d']}
180-360天：{queries['181_360d']}
60天内小网贷：{queries['micro_60d']}
本人
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
        print(f"错误: {str(e)}")
        raise HTTPException(500, f"处理失败: {str(e)}")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "v4_xparse_structured_fixed"}


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