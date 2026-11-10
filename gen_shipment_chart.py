# -*- coding: utf-8 -*-
"""从服务器DB拉取所有发货批次数据，生成收货地址柱状图"""
import json
import subprocess

# 从服务器拉数据
cmd = [
    'ssh', '-i', '/c/Users/lds/.ssh/consolidation_backup',
    'root@YOUR_SERVER_IP',
    r"""sqlite3 /opt/consolidation/consolidation.db "SELECT c.name, c.address, c.zip_code, COUNT(ppi.id) as cnt FROM packed_package_items ppi JOIN packed_packages pp ON ppi.packed_package_id = pp.id JOIN customers c ON pp.customer_id = c.id GROUP BY c.id ORDER BY cnt DESC" """
]

result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
lines = result.stdout.strip().split('\n')

stats = []
for line in lines:
    if not line.strip():
        continue
    parts = line.split('|')
    if len(parts) >= 4:
        stats.append({
            'name': parts[0],
            'address': parts[1],
            'zip_code': parts[2],
            'count': int(parts[3])
        })

total_customers = len(stats)
total_packages = sum(s['count'] for s in stats)
print(f"服务器数据: {total_customers} 个客户, {total_packages} 个包裹")

# 地址分布汇总
addr_dist = {}
for s in stats:
    addr = s['address']
    if addr not in addr_dist:
        addr_dist[addr] = 0
    addr_dist[addr] += s['count']
print(f"地址分布: {len(addr_dist)} 个不同收货地址")

# 柱状图只显示前50个
chart_data = stats[:50]
labels = json.dumps([s['name'] + '\n' + s['address'][:8] for s in chart_data], ensure_ascii=False)
data_values = json.dumps([s['count'] for s in chart_data])
chart_max = max(s['count'] for s in chart_data)

# 地址柱状图
addr_sorted = sorted(addr_dist.items(), key=lambda x: x[1], reverse=True)
addr_labels = json.dumps([a[0] for a in addr_sorted], ensure_ascii=False)
addr_values = json.dumps([a[1] for a in addr_sorted])
addr_max = max(addr_dist.values())

# 顶级分类
top_count = sum(1 for s in stats if s['count'] >= 100)
mid_count = sum(1 for s in stats if 10 <= s['count'] < 100)
low_count = sum(1 for s in stats if s['count'] < 10)

html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>历史发货批次 · 收货地址包裹统计</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    color: #e2e8f0;
    min-height: 100vh;
    padding: 32px 20px;
}}
.container {{ max-width: 1500px; margin: 0 auto; }}
.header {{ text-align: center; margin-bottom: 28px; }}
.header h1 {{
    font-size: 26px; font-weight: 700;
    background: linear-gradient(135deg, #667eea, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header .sub {{ font-size: 13px; color: #94a3b8; margin-top: 6px; }}
.stats-row {{
    display: flex; justify-content: center; gap: 24px; margin-top: 14px; flex-wrap: wrap;
}}
.stat-card {{
    background: rgba(30,41,59,0.8); border: 1px solid rgba(148,163,184,0.15);
    border-radius: 12px; padding: 16px 24px; text-align: center; min-width: 120px;
}}
.stat-card .num {{ font-size: 28px; font-weight: 800; color: #667eea; }}
.stat-card .label {{ font-size: 12px; color: #94a3b8; margin-top: 2px; }}
.stat-card.warn .num {{ color: #fbbf24; }}
.stat-card.danger .num {{ color: #f87171; }}
.chart-row {{
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 20px; margin-bottom: 24px;
}}
@media (max-width: 1000px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
.chart-card {{
    background: rgba(30,41,59,0.8); border: 1px solid rgba(148,163,184,0.15);
    border-radius: 16px; padding: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.3);
}}
.chart-card h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #cbd5e1; }}
.chart-wrap {{ position: relative; height: 500px; }}
.table-card {{
    background: rgba(30,41,59,0.8); border: 1px solid rgba(148,163,184,0.15);
    border-radius: 16px; padding: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.3);
    overflow: hidden;
}}
.table-card h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 14px; color: #cbd5e1; }}
.table-wrap {{ overflow-x: auto; max-height: 700px; overflow-y: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
thead {{ position: sticky; top: 0; z-index: 1; }}
thead th {{
    text-align: left; padding: 9px 12px; background: #1e293b;
    color: #94a3b8; font-weight: 600; border-bottom: 1px solid rgba(148,163,184,0.12);
    white-space: nowrap;
}}
tbody td {{ padding: 7px 12px; border-bottom: 1px solid rgba(148,163,184,0.06); color: #cbd5e1; }}
tbody tr:hover {{ background: rgba(102,126,234,0.06); }}
.bar-cell {{ display: flex; align-items: center; gap: 6px; }}
.bar-visual {{ height: 14px; background: linear-gradient(90deg, #667eea, #a78bfa); border-radius: 7px; }}
.tag {{
    display: inline-block; padding: 1px 7px; border-radius: 10px;
    font-size: 10px; font-weight: 700;
}}
.tag-t1 {{ background: rgba(248,113,113,0.15); color: #f87171; }}
.tag-t2 {{ background: rgba(251,191,36,0.15); color: #fbbf24; }}
.tag-t3 {{ background: rgba(102,126,234,0.15); color: #667eea; }}
.footer {{ text-align: center; padding: 24px; font-size: 11px; color: #475569; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>所有历史发货批次 · 收货地址包裹统计</h1>
    <div class="sub">基于 packed_package_items 数据（所有已完成发货的包裹）</div>
    <div class="stats-row">
      <div class="stat-card">
        <div class="num">{total_customers}</div>
        <div class="label">发货客户数</div>
      </div>
      <div class="stat-card">
        <div class="num">{total_packages}</div>
        <div class="label">发货包裹总数</div>
      </div>
      <div class="stat-card">
        <div class="num">{len(addr_dist)}</div>
        <div class="label">不同收货地址</div>
      </div>
      <div class="stat-card danger">
        <div class="num">{top_count}</div>
        <div class="label">≥100件客户</div>
      </div>
      <div class="stat-card warn">
        <div class="num">{mid_count}</div>
        <div class="label">10~99件客户</div>
      </div>
      <div class="stat-card">
        <div class="num">{low_count}</div>
        <div class="label">&lt;10件客户</div>
      </div>
    </div>
  </div>

  <div class="chart-row">
    <div class="chart-card">
      <h2>📊 按客户统计（TOP 50）</h2>
      <div class="chart-wrap"><canvas id="customerChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>📍 按收货地址汇总</h2>
      <div class="chart-wrap"><canvas id="addressChart"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <h2>📋 完整列表（{total_customers} 个客户）</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>客户名</th><th>收货地址</th><th>邮编</th>
            <th>包裹数</th><th>占比</th><th>分类</th><th>地址排名</th>
          </tr>
        </thead>
        <tbody>
'''

# 地址排名
addr_rank = {}
for idx, (addr, cnt) in enumerate(addr_sorted):
    addr_rank[addr] = idx + 1

for i, s in enumerate(stats):
    pct = s['count'] / total_packages * 100
    if s['count'] >= 100:
        tier, tag = '🔥 超高频', 'tag-t1'
    elif s['count'] >= 10:
        tier, tag = '⭐ 中频', 'tag-t2'
    else:
        tier, tag = '💤 低频', 'tag-t3'
    bar_w = max(pct * 3, 2)
    ar = addr_rank.get(s['address'], '-')
    html += f'''<tr>
      <td>{i+1}</td>
      <td><b>{s['name']}</b></td>
      <td>{s['address']}</td>
      <td>{s['zip_code']}</td>
      <td><b>{s['count']}</b></td>
      <td><div class="bar-cell"><div class="bar-visual" style="width:{bar_w}px"></div>{pct:.1f}%</div></td>
      <td><span class="tag {tag}">{tier}</span></td>
      <td>{ar}</td>
    </tr>
'''

html += '''
        </tbody>
      </table>
    </div>
  </div>

  <div class="footer">
    数据来源：服务器 YOUR_SERVER_IP · 集运管理系统 · 生成时间 2026-07-01
  </div>
</div>

<script>
const customerLabels = ''' + labels + ''';
const customerValues = ''' + data_values + ''';
const addrLabels = ''' + addr_labels + ''';
const addrValues = ''' + addr_values + ''';

const darkGrid = { color: 'rgba(148,163,184,0.06)' };
const darkTick = { color: '#94a3b8', font: { size: 11 } };

// 客户柱状图
new Chart(document.getElementById('customerChart'), {
  type: 'bar',
  data: {
    labels: customerLabels,
    datasets: [{
      label: '发货包裹数',
      data: customerValues,
      backgroundColor: customerValues.map(v => {
        const r = v / ''' + str(chart_max) + ''';
        if (r > 0.3) return 'rgba(248,113,113,0.75)';
        if (r > 0.05) return 'rgba(251,191,36,0.65)';
        return 'rgba(102,126,234,0.5)';
      }),
      borderColor: customerValues.map(v => {
        const r = v / ''' + str(chart_max) + ''';
        if (r > 0.3) return 'rgba(248,113,113,0.95)';
        if (r > 0.05) return 'rgba(251,191,36,0.85)';
        return 'rgba(102,126,234,0.7)';
      }),
      borderWidth: 1, borderRadius: 3, maxBarThickness: 28
    }]
  },
  options: {
    indexAxis: 'y',
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: darkTick, grid: darkGrid, beginAtZero: true, title: { display: true, text: '包裹数量', color: '#94a3b8' } },
      y: { ticks: { ...darkTick, font: { size: 10 }, autoSkip: false }, grid: { display: false } }
    }
  }
});

// 地址柱状图
new Chart(document.getElementById('addressChart'), {
  type: 'bar',
  data: {
    labels: addrLabels,
    datasets: [{
      label: '包裹总数',
      data: addrValues,
      backgroundColor: addrValues.map(v => {
        const r = v / ''' + str(addr_max) + ''';
        if (r > 0.2) return 'rgba(16,185,129,0.7)';
        if (r > 0.05) return 'rgba(59,130,246,0.55)';
        return 'rgba(139,92,246,0.4)';
      }),
      borderColor: addrValues.map(v => {
        const r = v / ''' + str(addr_max) + ''';
        if (r > 0.2) return 'rgba(16,185,129,0.9)';
        if (r > 0.05) return 'rgba(59,130,246,0.75)';
        return 'rgba(139,92,246,0.6)';
      }),
      borderWidth: 1, borderRadius: 4, maxBarThickness: 40
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      y: { ticks: darkTick, grid: darkGrid, beginAtZero: true, title: { display: true, text: '包裹数量', color: '#94a3b8' } },
      x: { ticks: { ...darkTick, font: { size: 11 } }, grid: { display: false } }
    }
  }
});
</script>
</body>
</html>
'''

desktop = r'C:\Users\lds\Desktop\历史发货批次_收货地址统计.html'
with open(desktop, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n柱状图已生成: {desktop}")
print(f"包含 {total_customers} 个客户, {len(addr_dist)} 个收货地址, {total_packages} 个包裹")
