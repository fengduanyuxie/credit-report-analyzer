# main.py
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

# 国有/股份制银行关键词（用于排除小网贷误判）
BANK_KEYWORDS = [
    "工商银行", "农业银行", "中国银行", "建设银行", "交通银行",
    "招商银行", "浦发银行", "中信银行", "光大银行", "华夏银行",
    "民生银行", "广发银行", "平安银行", "兴业银行", "浙商银行",
    "邮储银行", "北京银行", "上海银行", "江苏银行", "宁波银行",
    "南京银行", "杭州银行", "南昌农村商业银行", "江西万载农村商业银行"
]


def clean_number(num_str: str) -> float:
    if not num_str:
        return 0.0
    cleaned = num_str.replace(' ', '').replace('，', '').replace(',', '')
    try:
        return float(cleaned)
    except:
        return 0.0


def parse_pdf_with_xparse(pdf_bytes: bytes) -> Dict[str, Any]:
    headers = {
        "x-ti-app-id": TEXTIN_APP_ID,
        "x-ti-secret-code": TEXTIN_SECRET_CODE,
    }
    config = {
        "capabilities": {
            "include_table_structure": True,
            "pages": True,
            "include_hierarchy": True
        }
    }
    files = {"file": ("report.pdf", pdf_bytes, "application/pdf")}
    data = {"config": json.dumps(config)}
    response = requests.post(
        XPARSE_API_URL,
        headers=headers,
        files=files,
        data=data,
        timeout=60
    )
    if response.status_code != 200:
        raise Exception(f"xParse API HTTP错误: {response.status_code}")
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
    # 优先匹配已知小贷关键词
    for kw in MICRO_KEYWORDS:
        if kw in institution_name:
            return True
    # 如果包含银行关键词，不算小网贷
    for bk in BANK_KEYWORDS:
        if bk in institution_name:
            return False
    # 不含"银行"的非银行机构，算小网贷
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


def extract_public_records(elements: List[Dict]) -> str:
    """
    从 elements 中提取公共记录（支持跨页表格）
    """
    records = []
    
    # 欠税记录（markdown）
    for element in elements:
        text = element.get("text", "")
        if "欠税总额" in text:
            match = re.search(r'欠税总额[：:]\s*([\d,]+)', text)
            if match:
                amount = clean_number(match.group(1))
                records.append(f"欠税1条，金额{amount/10000:.2f}万元")
                break
    
    # 民事判决记录（支持跨页表格）
    judgment_total = 0
    judgment_count = 0
    in_judgment = False
    for element in elements:
        elem_type = element.get("type", "")
        text = element.get("text", "")
        
        if "民事判决记录" in text and elem_type == "Title":
            in_judgment = True
            continue
        
        if in_judgment and elem_type == "Table":
            table_structure = element.get("table_structure", {})
            cells = table_structure.get("cells", [])
            for cell in cells:
                cell_text = cell.get("text", "")
                match = re.search(r'诉讼标的金额[：:]\s*([\d,]+)', cell_text)
                if match:
                    judgment_total += clean_number(match.group(1))
                    judgment_count += 1
        elif in_judgment and "强制执行记录" in text:
            break
    
    if judgment_count > 0:
        records.append(f"民事判决{judgment_count}件，金额{judgment_total/10000:.2f}万元")
    
    # 强制执行记录（支持跨页表格）
    enforcement_total = 0
    enforcement_count = 0
    in_enforcement = False
    for element in elements:
        elem_type = element.get("type", "")
        text = element.get("text", "")
        
        if "强制执行记录" in text and elem_type == "Title":
            in_enforcement = True
            continue
        
        if in_enforcement and elem_type == "Table":
            table_structure = element.get("table_structure", {})
            cells = table_structure.get("cells", [])
            for cell in cells:
                cell_text = cell.get("text", "")
                # 优先取"申请执行标的金额"
                match = re.search(r'申请执行标的金额[：:]\s*([\d,]+)', cell_text)
                if match:
                    enforcement_total += clean_number(match.group(1))
                    enforcement_count += 1
        elif in_enforcement and "行政处罚记录" in text:
            break
    
    if enforcement_count > 0:
        records.append(f"强制执行{enforcement_count}件，金额{enforcement_total/10000:.2f}万元")
    
    # 行政处罚记录
    for element in elements:
        text = element.get("text", "")
        if "处罚金额" in text:
            match = re.search(r'处罚金额[：:]\s*([\d,]+)', text)
            if match:
                amount = clean_number(match.group(1))
                records.append(f"行政处罚1条，金额{amount/10000:.2f}万元")
                break
    
    return "\n".join(records) if records else "无"


def extract_loans_from_elements(elements: List[Dict]) -> Dict[str, Any]:
    """
    内容特征解析：直接扫描所有 NarrativeText，通过内容模式识别贷款
    修正：已结清账户严格排除
    """
    loans = {
        "count": 0, "balance": 0.0,
        "housing_count": 0, "housing_balance": 0.0,
        "car_count": 0, "car_balance": 0.0,
        "micro_count": 0, "micro_balance": 0.0,
        "overdue_count": 0
    }
    
    seen_ids = set()
    
    for element in elements:
        elem_type = element.get("type", "")
        elem_id = element.get("element_id", "")
        
        if elem_type != "NarrativeText":
            continue
        
        if elem_id in seen_ids:
            continue
        seen_ids.add(elem_id)
        
        text = element.get("text", "")
        lines = text.split('\n')
        
        for line in lines:
            line = line.strip()
            # 必须是序号开头的行
            if not re.match(r'^\d+\.', line):
                continue
            
            # 必须包含"发放"或"授信"
            if "发放" not in line and "授信" not in line:
                continue
            
            # 【修复1】更严格的已结清检测（支持换行和空格）
            if re.search(r'已\s*结\s*清', line) or "已转出" in line or "销户" in line:
                continue
            
            # 提取余额（支持"余额"和"余额为"）
            balance_match = re.search(r'余额[为]?([\d,]+)', line)
            balance = clean_number(balance_match.group(1)) if balance_match else 0
            
            # 提取机构名
            inst_match = re.search(r'\d{4}年\d{1,2}月\d{1,2}日([^发放授信]+?)(?:发放|为)', line)
            institution = inst_match.group(1).strip() if inst_match else ''
            
            # 只要是未结清的贷款/授信账户，都计入机构数
            loans["count"] += 1
            
            # 只有余额>0时才累加余额
            if balance > 0:
                loans["balance"] += balance / 10000
            
            # 分类
            is_housing = any(kw in line for kw in HOUSING_KEYWORDS)
            is_car = any(kw in line for kw in CAR_KEYWORDS)
            is_micro = is_micro_institution(institution) and not is_housing and not is_car
            
            if is_housing:
                loans["housing_count"] += 1
                if balance > 0:
                    loans["housing_balance"] += balance / 10000
            elif is_car:
                loans["car_count"] += 1
                if balance > 0:
                    loans["car_balance"] += balance / 10000
            elif is_micro:
                loans["micro_count"] += 1
                if balance > 0:
                    loans["micro_balance"] += balance / 10000
            
            if "当前有逾期" in line:
                loans["overdue_count"] += 1
    
    return loans


def extract_credits_from_elements(elements: List[Dict]) -> Dict[str, Any]:
    """
    内容特征解析：直接扫描所有 NarrativeText，通过内容模式识别信用卡
    """
    credits = {
        "count": 0, "limit": 0.0, "used": 0.0, "overdue": 0,
        "abnormal": {"stop_payment": 0, "frozen": 0, "doubtful": 0}
    }
    
    seen_ids = set()
    
    for element in elements:
        elem_type = element.get("type", "")
        elem_id = element.get("element_id", "")
        
        if elem_type != "NarrativeText":
            continue
        
        if elem_id in seen_ids:
            continue
        seen_ids.add(elem_id)
        
        text = element.get("text", "")
        lines = text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line or not re.match(r'^\d+\.', line):
                continue
            
            # 必须包含"贷记卡"和"人民币"
            if '贷记卡' not in line or '人民币' not in line:
                continue
            if '美元' in line:
                continue
            if '尚未激活' in line:
                continue
            if '销户' in line:
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


def extract_guarantee_from_elements(elements: List[Dict]) -> Tuple[int, float]:
    """
    从 elements 中提取担保信息（支持表格和段落文本）
    """
    count = 0
    balance = 0.0
    
    # 方法1：从表格中提取
    for element in elements:
        if element.get("type") != "Table":
            continue
        
        table_structure = element.get("table_structure", {})
        cells = table_structure.get("cells", [])
        if not cells:
            continue
        
        rows = {}
        for cell in cells:
            row_num = cell.get("row", 0)
            col_num = cell.get("col", 0)
            cell_text = cell.get("text", "").strip()
            if row_num not in rows:
                rows[row_num] = {}
            rows[row_num][col_num] = cell_text
        
        for row_data in rows.values():
            row_text = " ".join(str(v) for v in row_data.values())
            if "相关还款责任金额" not in row_text:
                continue
            
            count += 1
            amount_match = re.search(r'相关还款责任金额([\d,]+)', row_text)
            if amount_match:
                amount = clean_number(amount_match.group(1))
                balance_match = re.search(r'余额([\d,]+)', row_text)
                if balance_match:
                    loan_balance = clean_number(balance_match.group(1))
                    min_value = min(amount, loan_balance) if amount > 0 and loan_balance > 0 else amount
                    balance += min_value / 10000
                else:
                    balance += amount / 10000
    
    # 方法2：从段落文本中提取（企业担保）
    for element in elements:
        if element.get("type") != "NarrativeText":
            continue
        
        text = element.get("text", "")
        # 匹配担保信息模式
        if "承担相关还款责任" not in text:
            continue
        
        # 提取金额
        amount_match = re.search(r'相关还款责任金额\s*([\d,]+)', text)
        if not amount_match:
            amount_match = re.search(r'相关还款责任金额\s*--', text)
            if amount_match:
                continue
        
        if amount_match:
            count += 1
            amount = clean_number(amount_match.group(1))
            
            # 提取余额
            balance_match = re.search(r'贷款余额\s*([\d,]+)', text)
            if balance_match:
                loan_balance = clean_number(balance_match.group(1))
                min_value = min(amount, loan_balance) if amount > 0 and loan_balance > 0 else amount
                balance += min_value / 10000
            else:
                balance += amount / 10000
    
    return count, balance


def extract_queries_from_elements(elements: List[Dict], report_date: datetime) -> Dict[str, int]:
    """
    从 elements 中提取查询记录（支持跨页表格自动合并，动态识别列顺序）
    修复：识别"法人代表、负责人、高管等资信审查"等非标准查询原因
    """
    queries = {
        "30d": 0, "31_90d": 0, "91_180d": 0, "181_360d": 0,
        "micro_60d": 0, "self_60d": 0
    }
    
    inst_rows = []
    self_rows = []
    
    for element in elements:
        if element.get("type") != "Table":
            continue
        
        table_structure = element.get("table_structure", {})
        cells = table_structure.get("cells", [])
        if not cells:
            continue
        
        rows = {}
        for cell in cells:
            row_num = cell.get("row", 0)
            col_num = cell.get("col", 0)
            cell_text = cell.get("text", "").strip()
            if row_num not in rows:
                rows[row_num] = {}
            rows[row_num][col_num] = cell_text
        
        # 判断表格类型
        is_inst_table = False
        is_self_table = False
        
        for row_data in rows.values():
            row_text = " ".join(str(v) for v in row_data.values())
            # 【修复3】扩展机构查询识别：包含这些关键词的视为机构查询表
            if ("贷款审批" in row_text or "信用卡审批" in row_text or 
                "贷后管理" in row_text or "资信审查" in row_text or
                "担保资格审查" in row_text or "保前审查" in row_text):
                is_inst_table = True
            if "本人查询" in row_text:
                is_self_table = True
        
        # 动态识别列顺序
        col_map = {"date": None, "institution": None, "reason": None}
        header_row = rows.get(1, {})
        if header_row:
            for col_num, header_text in header_row.items():
                if "查询日期" in header_text or "日期" in header_text:
                    col_map["date"] = col_num
                elif "查询机构" in header_text or "机构" in header_text:
                    col_map["institution"] = col_num
                elif "查询原因" in header_text or "原因" in header_text:
                    col_map["reason"] = col_num
        
        if col_map["date"] is None:
            col_map["date"] = 2
        if col_map["institution"] is None:
            col_map["institution"] = 3
        if col_map["reason"] is None:
            col_map["reason"] = 4
        
        if is_inst_table:
            for row_num, row_data in rows.items():
                if row_num == 1 and header_row:
                    continue
                date = row_data.get(col_map["date"], "")
                institution = row_data.get(col_map["institution"], "")
                reason = row_data.get(col_map["reason"], "")
                if not date or not reason:
                    continue
                inst_rows.append((date, institution, reason))
        
        if is_self_table:
            for row_num, row_data in rows.items():
                if row_num == 1 and header_row:
                    continue
                date = row_data.get(col_map["date"], "")
                if not date:
                    continue
                self_rows.append(date)
    
    # 统计机构查询
    for date, institution, reason in inst_rows:
        # 排除贷后管理
        if "贷后管理" in reason:
            continue
        
        try:
            date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date)
            if date_match:
                y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                query_date = datetime(y, m, d)
                diff_days = (report_date - query_date).days
                if diff_days > 360:
                    continue
                if diff_days <= 30:
                    queries["30d"] += 1
                elif diff_days <= 90:
                    queries["31_90d"] += 1
                elif diff_days <= 180:
                    queries["91_180d"] += 1
                elif diff_days <= 360:
                    queries["181_360d"] += 1
                if diff_days <= 60 and institution:
                    if is_micro_institution(institution):
                        queries["micro_60d"] += 1
        except:
            pass
    
    # 统计本人查询
    for date in self_rows:
        try:
            date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date)
            if date_match:
                y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                query_date = datetime(y, m, d)
                diff_days = (report_date - query_date).days
                if 0 <= diff_days <= 60:
                    queries["self_60d"] += 1
        except:
            pass
    
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
        # 移除换行符，用分号连接
        records_str = public_records.replace("\n", "；")
        warnings.append(records_str)
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
        xparse_data = parse_pdf_with_xparse(pdf_bytes)
        markdown_text = xparse_data.get("markdown", "")
        elements = xparse_data.get("elements", [])
        
        print(f"xParse 解析成功，页数: {xparse_data.get('success_count', 0)}，元素数: {len(elements)}")
        
        report_date = extract_report_date_from_text(markdown_text)
        gender = extract_gender(markdown_text)
        age = extract_age(markdown_text, report_date)
        marriage = extract_marriage(markdown_text)
        
        loans = extract_loans_from_elements(elements)
        credits = extract_credits_from_elements(elements)
        guarantee_count, guarantee_balance = extract_guarantee_from_elements(elements)
        
        overdue = extract_overdue(markdown_text)
        asset_count, asset_balance = extract_asset_disposal(markdown_text)
        advance_count, advance_amount = extract_advance_payment(markdown_text)
        public_records = extract_public_records(elements)
        
        queries = extract_queries_from_elements(elements, report_date)
        
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
    return {"status": "ok", "version": "v8_mobile_ui"}


@app.get("/")
def frontend():
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=yes, viewport-fit=cover">
    <title>征信报告分析系统</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f5f7fa;
            padding: 16px;
            min-height: 100vh;
        }

        .container {
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 24px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
        }

        h1 {
            color: #1e3c72;
            border-bottom: 3px solid #4a90e2;
            padding-bottom: 12px;
            margin-bottom: 16px;
            font-size: 22px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .desc {
            color: #666;
            font-size: 14px;
            line-height: 1.5;
            margin-bottom: 20px;
            padding: 0 4px;
        }

        .upload-area {
            border: 2px dashed #4a90e2;
            border-radius: 20px;
            padding: 40px 20px;
            text-align: center;
            background: #fafcff;
            margin: 16px 0;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .upload-area:hover {
            background: #eef4ff;
            border-color: #357abd;
        }

        .upload-area p {
            color: #4a90e2;
            font-size: 16px;
        }

        .upload-area .file-name {
            color: #333;
            font-size: 14px;
            margin-top: 8px;
            word-break: break-all;
        }

        input[type="file"] {
            display: none;
        }

        button {
            background: #4a90e2;
            color: white;
            border: none;
            padding: 14px 28px;
            border-radius: 40px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            width: 100%;
            transition: background 0.2s ease;
            margin-top: 8px;
        }

        button:hover {
            background: #357abd;
        }

        button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }

        .loading {
            display: none;
            text-align: center;
            margin: 24px 0;
            color: #4a90e2;
            font-size: 14px;
        }

        .loading::before {
            content: "⏳";
            display: inline-block;
            animation: spin 1s linear infinite;
            margin-right: 8px;
        }

        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }

        .result-container {
            display: none;
            margin-top: 24px;
        }

        .result {
            background: #f9f9f9;
            border-radius: 16px;
            padding: 16px;
            font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
            font-size: 12px;
            line-height: 1.6;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 500px;
            overflow-y: auto;
            border: 1px solid #e0e0e0;
        }

        .result::-webkit-scrollbar {
            width: 6px;
        }

        .result::-webkit-scrollbar-track {
            background: #f1f1f1;
            border-radius: 3px;
        }

        .result::-webkit-scrollbar-thumb {
            background: #ccc;
            border-radius: 3px;
        }

        .info-note {
            background: #e8f4fd;
            padding: 12px;
            border-radius: 12px;
            margin-top: 20px;
            font-size: 12px;
            color: #4a90e2;
            text-align: center;
        }

        .info-note a {
            color: #1e3c72;
        }

        .bottom-space {
            height: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <span>📄</span>
            征信报告AI分析
        </h1>
        <p class="desc">上传PDF格式的个人信用报告，系统将自动解析并生成专业风控报告。</p>

        <div class="upload-area" onclick="document.getElementById('file').click()">
            <p>📎 点击或拖拽上传PDF文件</p>
            <p class="file-name" id="fileName"></p>
            <input type="file" id="file" accept=".pdf">
        </div>

        <button id="analyzeBtn" disabled>开始分析</button>

        <div class="loading" id="loading">
            正在解析并分析报告，请稍候...（可能需要30-60秒）
        </div>

        <div class="result-container" id="resultContainer">
            <div class="result" id="result"></div>
        </div>

        <div class="info-note">
            💡 提示：分析结果包含两部分 — 简要汇总 + AI展开分析
        </div>
        <div class="bottom-space"></div>
    </div>

    <script>
        let selectedFile = null;
        const fileInput = document.getElementById('file');
        const uploadArea = document.querySelector('.upload-area');
        const analyzeBtn = document.getElementById('analyzeBtn');
        const loadingDiv = document.getElementById('loading');
        const resultDiv = document.getElementById('result');
        const resultContainer = document.getElementById('resultContainer');
        const fileNameSpan = document.getElementById('fileName');

        uploadArea.addEventListener('click', () => fileInput.click());

        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.style.background = '#eef4ff';
            uploadArea.style.borderColor = '#357abd';
        });

        uploadArea.addEventListener('dragleave', () => {
            uploadArea.style.background = '#fafcff';
            uploadArea.style.borderColor = '#4a90e2';
        });

        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.style.background = '#fafcff';
            uploadArea.style.borderColor = '#4a90e2';
            if (e.dataTransfer.files.length > 0) {
                handleFile(e.dataTransfer.files[0]);
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFile(e.target.files[0]);
            }
        });

        function handleFile(file) {
            if (file.type !== 'application/pdf') {
                alert('请上传PDF格式的文件');
                return;
            }
            selectedFile = file;
            analyzeBtn.disabled = false;
            fileNameSpan.innerHTML = `✅ 已选择：${file.name}`;
            uploadArea.querySelector('p:first-child').innerHTML = '📄 文件已就绪，点击可更换';
        }

        analyzeBtn.addEventListener('click', async () => {
            if (!selectedFile) return;

            analyzeBtn.disabled = true;
            loadingDiv.style.display = 'block';
            resultContainer.style.display = 'none';
            resultDiv.innerText = '';

            const formData = new FormData();
            formData.append('file', selectedFile);

            try {
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.detail || '分析失败');
                }

                resultDiv.innerText = data.full_report;
                resultContainer.style.display = 'block';

                resultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });

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