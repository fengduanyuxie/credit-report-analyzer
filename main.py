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