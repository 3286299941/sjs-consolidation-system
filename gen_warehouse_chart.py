# -*- coding: utf-8 -*-
"""统计历史批次收货地址包裹数量，生成柱状图到桌面"""
import sqlite3
import json

DB_PATH = r'D:\pycharm\pythonProject\集运管理系统\consolidation.db'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 按客户（收货地址）统计所有入库包裹数（排除无主包裹）
cur.execute('''
    SELECT c.name, c.address, c.zip_code, COUNT(ip.id) as cnt
    FROM customers c
    JOIN incoming_packages ip ON ip.customer_id = c.id
    GROUP BY c.id
    ORDER BY cnt DESC
''')
rows = cur.fetchall()
conn.close()

stats = []
for r in rows:
    if r['cnt'] > 0:
        stats.append({
            'name': r['name'],
            'address': r['address'] or '',
            'zip_code': r['zip_code'] or '',
            'count': r['cnt']
        })

total_customers = len(stats)
total_packages = sum(s['count'] for s in stats)
print(f"共 {total_customers} 个客户，总包裹数 {total_packages}")

# 生成 HTML 柱状图
labels = json.dumps([s['address'][:15] + ('...' if len(s['address'])>15 else '') for s in stats])
data_values = json.dumps([s['count'] for s in stats])

html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>仓库货架规划 · 收货地址包裹统计</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    color: #e2e8f0;
    min-height: 100vh;
    padding: 40px 20px;
}}
.container {{ max-width: 1400px; margin: 0 auto; }}
.header {{ text-align: center; margin-bottom: 32px; }}
.header h1 {{
    font-size: 28px; font-weight: 700;
    background: linear-gradient(135deg, #667eea, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header .stats {{ display: flex; justify-content: center; gap: 32px; margin-top: 12px; font-size: 14px; color: #94a3b8; }}
.header .stats span b {{ color: #667eea; font-size: 20px; margin-right: 4px; }}
.chart-wrapper {{
    background: rgba(30, 41, 59, 0.8); border: 1px solid rgba(148, 163, 184, 0.15);
    border-radius: 16px; padding: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.3);
}}
.chart-container {{ position: relative; height: 600px; }}
.plan-section {{
    margin-top: 32px; background: rgba(30, 41, 59, 0.8);
    border: 1px solid rgba(148, 163, 184, 0.15);
    border-radius: 16px; padding: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.3);
}}
.plan-section h2 {{ font-size: 20px; font-weight: 700; margin-bottom: 16px; color: #a78bfa; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead th {{
    text-align: left; padding: 10px 14px; background: rgba(102,126,234,0.12);
    color: #94a3b8; font-weight: 600; border-bottom: 1px solid rgba(148,163,184,0.12); white-space: nowrap;
}}
tbody td {{ padding: 9px 14px; border-bottom: 1px solid rgba(148,163,184,0.06); color: #cbd5e1; }}
tbody tr:hover {{ background: rgba(102,126,234,0.06); }}
.bar-cell {{ display: flex; align-items: center; gap: 8px; }}
.bar-visual {{ height: 16px; background: linear-gradient(90deg, #667eea, #a78bfa); border-radius: 8px; max-width: 200px; }}
.count-badge {{ font-weight: 700; color: #667eea; min-width: 32px; text-align: right; }}
.tier-high {{ color: #f87171 !important; }}
.tier-mid {{ color: #fbbf24 !important; }}
.btn-row {{ display: flex; gap: 12px; margin-top: 16px; flex-wrap: wrap; }}
.btn-apply {{
    padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: all 0.2s; background: linear-gradient(135deg, #667eea, #764ba2); color: #fff;
}}
.btn-apply:hover {{ opacity: 0.85; transform: translateY(-1px); }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; margin-left: 4px; }}
.tag-frequently {{ background: rgba(248,113,113,0.15); color: #f87171; }}
.tag-normal {{ background: rgba(251,191,36,0.15); color: #fbbf24; }}
.tag-low {{ background: rgba(102,126,234,0.15); color: #667eea; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>仓库货架规划 · 收货地址包裹统计</h1>
    <div class="stats">
      <span>🛒 <b>{total_customers}</b> 个客户</span>
      <span>📦 <b>{total_packages}</b> 个包裹</span>
      <span>🗄 <b>108</b> 个可用区域（9架×4层×3区）</span>
    </div>
  </div>

  <div class="chart-wrapper">
    <div class="chart-container"><canvas id="barChart"></canvas></div>
  </div>

  <div class="plan-section">
    <h2>📋 货架分配建议（按包裹量降序 · 优先级从高到低）</h2>
    <div style="margin-bottom:14px;font-size:13px;color:#94a3b8">
      分类标准：<span class="tag tag-frequently">高频 ≥50件</span>
      <span class="tag tag-normal">中频 10~49件</span>
      <span class="tag tag-low">低频 &lt;10件</span>
      &nbsp;· 高频客户建议独占一层/区域，中频按需分配，低频合并共享
    </div>
    <div class="btn-row">
      <button class="btn-apply" onclick="generatePlan()">🔄 重新生成分配方案</button>
      <button class="btn-apply" id="btn-copy" onclick="copyPlan()">📋 复制分配方案</button>
    </div>
    <div class="table-wrap" style="margin-top:16px">
      <table id="planTable">
        <thead>
          <tr>
            <th>优先级</th><th>客户</th><th>收货地址</th><th>邮编</th>
            <th>包裹数</th><th>占比</th><th>分类</th><th>分配货架区域</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
    <div style="margin-top:12px;font-size:12px;color:#64748b" id="free-slots"></div>
  </div>
</div>

<script>
const CUSTOMERS = {json.dumps(stats)};
const TOTAL = {total_packages};

const rows = ['A','B','C'], cols = [1,2,3], floors = [1,2,3,4], zones = ['L','M','R'];
let ALL_SLOTS = [];
for (let r of rows) for (let c of cols) for (let f of floors) for (let z of zones) {{
  ALL_SLOTS.push(`${{r}}${{c}}-${{f}}-${{z}}`);
}}

// 分类函数
function classify(cnt) {{
  if (cnt >= 50) return {{ tier: '高频', cls: 'tag-frequently', priority: 1 }};
  if (cnt >= 10) return {{ tier: '中频', cls: 'tag-normal', priority: 2 }};
  return {{ tier: '低频', cls: 'tag-low', priority: 3 }};
}}

function generatePlan() {{
  const sorted = [...CUSTOMERS].sort((a,b) => b.count - a.count);

  // 分配策略：
  // 高频(>=50)：独占一整层（3个区域）=同一货架的同一层 L/M/R
  // 中频(10-49)：按20件/区域，优先由同一货架分配
  // 低频(<10)：按地址相近或邮编相同合并共享
  let slots = [...ALL_SLOTS];
  let plan = [];

  sorted.forEach((cust, idx) => {{
    let entry = {{ ...cust, slots: [], priority: idx + 1 }};
    let info = classify(cust.count);

    if (info.tier === '高频') {{
      // 找一整层（同一货架同一层的 L,M,R）
      const needed = Math.max(1, Math.ceil(cust.count / 30));
      for (let f = 1; f <= 4 && entry.slots.length < needed; f++) {{
        for (let r of rows) {{
          for (let c of cols) {{
            let layerSlots = [`${{r}}${{c}}-${{f}}-L`, `${{r}}${{c}}-${{f}}-M`, `${{r}}${{c}}-${{f}}-R`];
            let allAvail = layerSlots.every(s => slots.includes(s));
            if (allAvail && entry.slots.length < needed) {{
              entry.slots.push(...layerSlots);
              layerSlots.forEach(s => slots.splice(slots.indexOf(s), 1));
            }}
          }}
        }}
      }}
      // 如果一整层不够，补单个区域
      while (entry.slots.length < needed && slots.length > 0) {{
        entry.slots.push(slots.shift());
      }}
    }} else if (info.tier === '中频') {{
      const needed = Math.max(1, Math.ceil(cust.count / 20));
      // 优先同一货架
      for (let r of rows) {{
        for (let c of cols) {{
          for (let f = 1; f <= 4 && entry.slots.length < needed; f++) {{
            for (let z of zones) {{
              let s = `${{r}}${{c}}-${{f}}-${{z}}`;
              if (slots.includes(s) && entry.slots.length < needed) {{
                entry.slots.push(s);
                slots.splice(slots.indexOf(s), 1);
              }}
            }}
          }}
        }}
      }}
      // 仍有需求的随便补
      while (entry.slots.length < needed && slots.length > 0) {{
        entry.slots.push(slots.shift());
      }}
    }} else {{
      // 低频：合并共享，与上一个低频客户放一起（如果还有空间）
      // 这里简单处理：每个低频客户给1个区域
      if (slots.length > 0) {{
        entry.slots.push(slots.shift());
      }}
    }}

    plan.push(entry);
  }});

  renderTable(plan, slots);
  return {{ plan, remaining: slots }};
}}

function renderTable(plan, remaining) {{
  const tbody = document.getElementById('planTable').querySelector('tbody');
  let html = '';
  plan.forEach((cust, i) => {{
    const pct = (cust.count / TOTAL * 100).toFixed(1);
    const info = classify(cust.count);
    html += `<tr>
      <td>${{i+1}}</td>
      <td><b>${{cust.name}}</b></td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{cust.address}}">${{cust.address}}</td>
      <td>${{cust.zip_code}}</td>
      <td><span class="count-badge ${{info.priority===1?'tier-high':info.priority===2?'tier-mid':''}}">${{cust.count}}</span></td>
      <td><div class="bar-cell"><div class="bar-visual" style="width:${{Math.max(pct*2.5,3)}}px"></div><span>${{pct}}%</span></div></td>
      <td><span class="tag ${{info.cls}}">${{info.tier}}</span></td>
      <td style="font-family:monospace;font-size:11px;color:#667eea">${{cust.slots.join(', ') || '(无)'}}</td>
    </tr>`;
  }});
  tbody.innerHTML = html;
  document.getElementById('free-slots').textContent =
    remaining.length > 0
      ? `🟢 剩余 ${{remaining.length}} 个空闲区域：${{remaining.join(', ')}}`
      : '🔴 所有区域已分配完毕！';
}}

function copyPlan() {{
  let lines = [];
  const rows = document.querySelectorAll('#planTable tbody tr');
  rows.forEach(r => {{
    let cells = r.querySelectorAll('td');
    if (cells.length >= 7) {{
      lines.push(`${{cells[1].textContent.trim()}}\\t${{cells[2].textContent.trim()}}\\t${{cells[3].textContent.trim()}}\\t${{cells[4].textContent.trim()}}\\t${{cells[6].textContent.trim()}}\\t${{cells[7].textContent.trim()}}`);
    }}
  }});
  navigator.clipboard.writeText(lines.join('\\n')).then(() => alert('已复制到剪贴板'));
}}

// Chart.js 柱状图
new Chart(document.getElementById('barChart'), {{
  type: 'bar',
  data: {{
    labels: {labels},
    datasets: [{{
      label: '包裹数量',
      data: {data_values},
      backgroundColor: {data_values}.map(v => {{
        const ratio = v / Math.max(...{data_values});
        if (ratio > 0.4) return 'rgba(248,113,113,0.7)';
        if (ratio > 0.1) return 'rgba(251,191,36,0.7)';
        return 'rgba(102,126,234,0.55)';
      }}),
      borderColor: {data_values}.map(v => {{
        const ratio = v / Math.max(...{data_values});
        if (ratio > 0.4) return 'rgba(248,113,113,0.9)';
        if (ratio > 0.1) return 'rgba(251,191,36,0.9)';
        return 'rgba(102,126,234,0.8)';
      }}),
      borderWidth: 1, borderRadius: 4, maxBarThickness: 35
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          title: ctx => CUSTOMERS[ctx[0].dataIndex].name + ' - ' + CUSTOMERS[ctx[0].dataIndex].address,
          label: ctx => '包裹: ' + ctx.raw + ' 件 (' + (ctx.raw/TOTAL*100).toFixed(1) + '%)'
        }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8', font: {{ size: 10 }}, maxRotation: 60, autoSkip: true, maxTicksLimit: 45 }}, grid: {{ color: 'rgba(148,163,184,0.06)' }} }},
      y: {{ ticks: {{ color: '#94a3b8', font: {{ size: 11 }}, callback: v => v + ' 件' }}, grid: {{ color: 'rgba(148,163,184,0.08)' }}, beginAtZero: true }}
    }}
  }}
}});

// 初始加载
window.onload = () => generatePlan();
</script>
</body>
</html>
'''

desktop = r'C:\Users\lds\Desktop\仓库货架规划_收货地址统计.html'
with open(desktop, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n柱状图已生成: {desktop}")
