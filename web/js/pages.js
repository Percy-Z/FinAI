/* 页面渲染层：每个页面返回 HTML 字符串 + 可选的 mount 钩子。
   所有数字都来自 ctx.data（真实产物或内置演示数据），本文件不做估值计算。 */
(function (global) {
  'use strict';

  var esc = Charts.esc;
  var MULT_LABEL = { pe: 'PE', pb: 'PB', ps: 'PS', ev_ebitda: 'EV/EBITDA', ev_ebit: 'EV/EBIT' };

  /* ── 格式化 ───────────────────────────────────────────────────────── */
  var F = {
    money: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      d = d == null ? 2 : d;
      var a = Math.abs(v), unit, x;
      if (a >= 1e8) { unit = ' 亿元'; x = v / 1e8; }
      else if (a >= 1e4) { unit = ' 万元'; x = v / 1e4; }
      else { unit = ' 元'; x = v; }
      return x.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) + unit;
    },
    moneyShort: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      d = d == null ? 2 : d;
      var a = Math.abs(v);
      if (a >= 1e8) return (v / 1e8).toFixed(d);
      if (a >= 1e4) return (v / 1e4).toFixed(d);
      return v.toFixed(d);
    },
    moneyUnit: function (v) {
      if (v == null || !isFinite(v)) return '';
      var a = Math.abs(v);
      return a >= 1e8 ? '亿元' : a >= 1e4 ? '万元' : '元';
    },
    price: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      d = d == null ? 2 : d;
      return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
    },
    pct: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      d = d == null ? 2 : d;
      return (v * 100).toFixed(d) + '%';
    },
    pctSigned: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      var s = F.pct(v, d);
      return v > 0 ? '+' + s : s;
    },
    num: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      d = d == null ? 2 : d;
      return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
    },
    x: function (v, d) {
      if (v == null || !isFinite(v)) return '—';
      return (d == null ? 2 : d) === 0 ? String(Math.round(v)) : v.toFixed(d == null ? 2 : d);
    },
    int: function (v) { return v == null || !isFinite(v) ? '—' : Math.round(v).toLocaleString('zh-CN'); },
    bytes: function (n) {
      if (n == null || !isFinite(n)) return '—';
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      return (n / 1048576).toFixed(2) + ' MB';
    },
    dt: function (s) {
      if (!s) return '—';
      var t = String(s).replace('T', ' ').replace(/(\.\d+)?(Z|[+-]\d\d:?\d\d)$/, '');
      return t.length > 19 ? t.slice(0, 19) : t;
    },
    dur: function (s) {
      if (s == null) return '—';
      if (s < 1) return (s * 1000).toFixed(0) + ' ms';
      if (s < 60) return s.toFixed(2) + ' s';
      return Math.floor(s / 60) + ' 分 ' + (s % 60).toFixed(1) + ' 秒';
    },
    short: function (h, n) { return h ? String(h).slice(0, n || 10) : '—'; }
  };
  /* 配置里的部分参数是「逐年数组」（毛利率、费用率、税率等）。
     展示单个数值时取第一年，避免把数组当标量算出 NaN。 */
  F.one = function (v) {
    if (v == null) return null;
    if (Array.isArray(v)) return v.length ? v[0] : null;
    return v;
  };

  /* ── 组件小工具 ───────────────────────────────────────────────────── */
  function card(title, icon, body, opts) {
    opts = opts || {};
    var h = '<section class="card' + (opts.cls ? ' ' + opts.cls : '') + '">';
    if (title) {
      h += '<div class="card-h">' +
        (icon ? '<span class="card-title-ico' + (opts.aiIcon ? ' ai' : '') + '">' + Icons.svg(icon) + '</span>' : '') +
        '<h3>' + esc(title) + '</h3>' +
        (opts.sub ? '<span class="sub">' + opts.sub + '</span>' : '') +
        (opts.right ? '<span class="right">' + opts.right + '</span>' : '') +
        '</div>';
    }
    h += body + '</section>';
    return h;
  }

  function kpi(label, value, unit, foot, opts) {
    opts = opts || {};
    return '<div class="kpi' + (opts.hero ? ' hero' : '') + '">' +
      '<div class="kpi-label">' + (opts.icon ? Icons.svg(opts.icon) : '') + esc(label) + '</div>' +
      '<div class="kpi-value">' + value + (unit ? '<span class="unit">' + esc(unit) + '</span>' : '') + '</div>' +
      (foot ? '<div class="kpi-foot">' + foot + '</div>' : '') +
      '</div>';
  }

  function kvRows(rows) {
    return '<div class="kv">' + rows.filter(Boolean).map(function (r) {
      return '<div class="kv-row"><span class="kv-k">' + esc(r[0]) + '</span>' +
        '<span class="kv-v">' + r[1] + '</span></div>';
    }).join('') + '</div>';
  }

  function hashChip(h) {
    if (!h) return '<span class="muted">—</span>';
    return '<span class="hash" data-copy="' + esc(h) + '" title="点击复制完整值：' + esc(h) + '">' +
      esc(F.short(h, 12)) + '…</span>';
  }

  function evBtn(label, title, body) {
    return '<button class="badge code" type="button" data-ev="1" data-ev-title="' + esc(title) +
      '" data-ev-body="' + esc(encodeURIComponent(body)) + '">' + Icons.svg('hash') + esc(label) + '</button>';
  }

  /* 证据抽屉的内容：把「这个数字是怎么来的」摊开（PRD FE-I02 / FE-C01）。
     真实运行时给出产物文件与字段名，演示数据时明确标注来源是演示样本。 */
  function evPerShare(d, s) {
    var src = d.demo
      ? '内置演示样本（非真实产物）'
      : 'runs/' + d.run_id + '/03_valuation.json';
    return evBtn('证据', '每股价值是怎么算出来的',
      '<p><b>每股价值</b> = 权益价值 ÷ 摊薄股数</p>' +
      '<p><b>权益价值</b> = 企业价值 −（付息债务 − 现金）− 少数股东权益 − 优先股 + 非经营性资产</p>' +
      kvRows([
        ['企业价值', F.money(s.ev)],
        ['净债务', F.money(s.netDebt)],
        ['权益价值', F.money(s.equity)],
        ['摊薄股数', d.market && d.market.diluted_shares != null
          ? F.num(d.market.diluted_shares, 0) + ' 股' : '—'],
        ['每股价值', F.price(s.per) + ' 元'],
        ['当前股价', F.price(s.price) + ' 元']
      ]) +
      '<p class="muted small">来源：' + esc(src) +
      '，字段 <code class="inline">per_share / equity_value / net_debt / enterprise_value / diluted_shares</code>。' +
      '按仓库约定，金额单位为元、股数单位为股。</p>');
  }

  /* 估值区间的构成（PRD FE-C13 / M01-03） */
  function evFootballField(d, ff) {
    ff = ff || {};
    var src = d.demo ? '内置演示样本' : 'runs/' + d.run_id + '/09_football_field.json';
    var rows = (ff.bands || []).map(function (b) {
      return [b.name, F.price(b.low) + ' – ' + F.price(b.high) + '（中枢 ' + F.price(b.central) + '）'];
    });
    return evBtn('证据', '估值区间是怎么得到的',
      '<p>每条区间来自一种估值方法；<b>中枢</b>是点估计，<b>上下界</b>是不确定性范围。' +
      'DCF 的上下界取自单因素敏感性（04_tornado.csv）的极值，倍数法的上下界取自可比公司倍数的四分位区间。</p>' +
      (rows.length ? kvRows(rows) : '<p class="muted">本次没有区间数据。</p>') +
      '<p class="muted small">当前股价 ' + F.price(ff.price) + ' 元；整体区间 ' +
      F.price(ff.overall_low) + ' – ' + F.price(ff.overall_high) + ' 元。来源：' + esc(src) + '。</p>');
  }

  /* 单因素敏感性的口径（PRD FE-C05 / FE-C07） */
  function evTornado(d, top) {
    if (!top) return '';
    var src = d.demo ? '内置演示样本' : 'runs/' + d.run_id + '/04_tornado.csv';
    return evBtn('证据', '波动幅度是怎么算的',
      '<p>对每个假设单独取「下界」「上界」各算一次每股价值，两者之差就是<b>波动幅度</b>，' +
      '其余假设保持基准值不变——所以这是<b>单因素</b>敏感性，不是多因素联合。</p>' +
      kvRows([
        ['影响最大的假设', esc(top.name || '—')],
        ['下界 / 上界', F.pct(top.low) + ' / ' + F.pct(top.high)],
        ['下界估值', F.price(top.per_share_at_low) + ' 元'],
        ['上界估值', F.price(top.per_share_at_high) + ' 元'],
        ['波动幅度', F.price(top.swing) + ' 元']
      ]) +
      '<p class="muted small">上下界的浮动幅度由 [sensitivity] 配置决定。来源：' + esc(src) + '。</p>');
  }

  /* 反向求解的方法（PRD FE-C09 / FE-C10） */
  function evReverse(d, r) {
    if (!r) return '';
    var src = d.demo ? '内置演示样本' : 'runs/' + d.run_id + '/06_reverse_dcf.json';
    return evBtn('证据', '隐含值是怎么反解的',
      '<p>把当前股价当作目标，单独调整这一个变量、其余保持不变，用二分法求「使 DCF 每股价值等于股价」的取值。' +
      '它表示<b>孤立来看</b>市场需要相信什么，不等于市场真的这么认为。</p>' +
      kvRows([
        ['变量', esc(r.name)],
        ['我们的取值', F.pct(r.base_value)],
        ['市场隐含', r.implied_value == null ? '未收敛' : F.pct(r.implied_value)],
        ['搜索区间', r.lower_bound != null ? F.pct(r.lower_bound) + ' – ' + F.pct(r.upper_bound) : '—'],
        ['迭代次数', r.iterations != null ? String(r.iterations) : '—'],
        ['残差', r.residual != null ? String(r.residual) : '—']
      ]) +
      '<p class="muted small">永续增长率必须小于 WACC，否则终值不收敛；搜索区间会自动裁剪到这个范围内。' +
      '来源：' + esc(src) + '。</p>');
  }

  /* 倍数到每股价值的换算（PRD FE-C12 / FE-C15） */
  function evCompsImplied(d, x, netDebt, shares) {
    if (!x) return '';
    var src = d.demo ? '内置演示样本' : 'runs/' + d.run_id + '/08_comps_implied.csv';
    var isEquity = ['pe', 'pb', 'ps'].indexOf(x.multiple) >= 0;
    return evBtn('证据', '倍数是怎么换算成每股价值的',
      '<p>倍数分两类，换算路径<b>不一样</b>：</p>' +
      '<p>· <b>权益类</b>（PE / PB / PS）：倍数 × 目标公司每股指标 → 直接得到隐含股价。<br>' +
      '· <b>企业价值类</b>（EV/EBITDA、EV/EBIT）：倍数 × 目标公司指标 = 隐含企业价值，' +
      '再<b>减净债务</b>、减少数股东权益与优先股、加回非经营性资产，最后除以摊薄股数。</p>' +
      '<p>把 EV/EBITDA 直接乘到每股 EBITDA 上是最常见的错误，仓库里有专门的回归测试锁住这条。</p>' +
      kvRows([
        ['倍数 / 口径', esc(x.label || MULT_LABEL[x.multiple] || x.multiple) + ' · ' + esc(x.stat_name || 'median')],
        ['可比公司倍数', F.x(x.peer_value)],
        ['换算路径', isEquity ? '权益类：直接乘每股指标' : '企业价值类：先减净债务再除股数'],
        ['隐含每股价值', F.price(x.implied_per_share) + ' 元']
      ]) +
      '<p class="muted small">目标公司净债务 ' + F.money(netDebt) + '，摊薄股数 ' + F.num(shares, 0) +
      ' 股。来源：' + esc(src) + '。</p>');
  }

  function demoBanner(d) {
    if (!d.demo) return '';
    var why = d.demo_reason === 'no-run' ? '本地还没有任何运行记录'
      : d.demo_reason === 'load-failed' ? '没能读到这次运行的产物文件'
      : '当前展示的是内置演示数据';
    return '<div class="banner info">' + Icons.svg('info') +
      '<div><b>演示数据</b> — ' + why +
      '。下面所有数字均为演示样本，用于查看界面结构与交互；运行一次估值后会自动切换为真实产物。</div>' +
      (d.demo_reason === 'load-failed'
        ? '<button class="btn sm ghost" data-retry="1" type="button">重新连接</button>' : '') +
      '<button class="icon-btn b-close" data-dismiss="1" type="button" aria-label="关闭">' +
      Icons.svg('close') + '</button></div>';
  }

  function emptyBlock(icon, title, text, action) {
    return '<div class="empty"><span class="e-ico">' + Icons.svg(icon) + '</span>' +
      '<h3>' + esc(title) + '</h3><p>' + esc(text) + '</p>' + (action || '') + '</div>';
  }

  function statusBadge(s) {
    var m = { pass: ['pass', '✓ PASS'], warning: ['warn', '⚠ WARN'], fail: ['fail', '✕ FAIL'],
      skipped: ['skip', '○ SKIP'], ok: ['pass', '✓ OK'], failed: ['fail', '✕ FAILED'] };
    var x = m[s] || ['neutral', s || '—'];
    return '<span class="badge ' + x[0] + '">' + x[1] + '</span>';
  }

  function valSummary(d) {
    var v = d.valuation || {};
    return {
      per: v.per_share, price: v.price, ret: v.implied_return, ev: v.enterprise_value,
      equity: v.equity_value, netDebt: v.net_debt, wacc: v.wacc,
      tshare: v.terminal_share_of_ev, exitM: v.implied_exit_multiple,
      diag: v.diagnostics || []
    };
  }

  function narrativeOf(d) {
    return (d.manifest && d.manifest.inputs && d.manifest.inputs.narrative) || null;
  }

  /* ══════════════════════════════════════════════════════════════════
     页面 1：总览
     ══════════════════════════════════════════════════════════════════ */
  var dashboard = {
    id: 'dashboard', nav: '总览', icon: 'dashboard', group: '估值驾驶舱',
    title: '总览',
    sub: function (c) { return (c.data.company.name || '') + '（' + (c.data.company.code || '') + '）· 数据基准日 ' + (c.data.as_of || '—'); },
    render: function (c) {
      var d = c.data, s = valSummary(d), nv = narrativeOf(d), out = [];
      var band = d.football_field || {};
      var vsum = (d.validation && d.validation.reports && d.validation.reports[0]) ? d.validation.reports[0].summary : null;

      out.push(demoBanner(d));

      out.push('<div class="verdict">' +
        '<div><div class="kpi-label">' + Icons.svg('scale') + 'DCF 每股价值</div>' +
        '<div class="v-score">' + F.price(s.per) + '<span class="unit" style="font-size:14px;color:var(--text-2)"> 元</span></div></div>' +
        '<div class="v-text">相对现价 ' + F.price(s.price) + ' 元，隐含回报 ' +
        '<b class="' + (s.ret >= 0 ? 'delta up' : 'delta down') + '">' + F.pctSigned(s.ret) + '</b>；' +
        '各方法估值区间 ' + F.price(band.overall_low) + ' – ' + F.price(band.overall_high) + ' 元。</div>' +
        '<div class="v-meta">' +
        '<span class="badge code">' + Icons.svg('check') + '代码判断</span>' +
        (nv && nv.ok ? '<span class="badge ai">' + Icons.svg('sparkles') + 'AI 生成 · 结论</span>' : '') +
        (d.demo ? '<span class="badge neutral">演示数据</span>' : '<span class="badge pass">真实产物</span>') +
        '</div></div>');

      var revSeries = (d.projections && d.projections.years) || [];
      var sparkRev = revSeries.map(function (r) { return r.income.revenue / 1e8; });
      var sparkFcff = (d.valuation && d.valuation.projection && d.valuation.projection.fcff || []).map(function (x) { return x / 1e8; });

      out.push('<div class="grid g-4 mt14">' +
        kpi('DCF 每股价值', F.price(s.per), '元', '<span class="badge ' + (s.ret >= 0 ? 'up' : 'down') + '">' +
          F.pctSigned(s.ret) + '</span><span class="muted">相对现价</span>' + evPerShare(d, s),
          { hero: true, icon: 'target' }) +
        kpi('当前股价', F.price(s.price), '元', '<span class="muted">基准日 ' + esc(d.as_of || '—') + '</span>' +
          Charts.sparkline(sparkRev.length ? sparkRev : [1, 1.4, 1.8, 2.2, 2.6], Charts.colors.mint)) +
        kpi('企业价值', F.moneyShort(s.ev, 2), F.moneyUnit(s.ev), '<span class="muted">权益价值 ' +
          F.money(s.equity) + '</span>') +
        kpi('终值占企业价值', F.pct(s.tshare), '', '<span class="muted">隐含退出倍数 ' + F.x(s.exitM) + '×</span>' +
          Charts.sparkline(sparkFcff.length ? sparkFcff : [1, 1.2, 1.4, 1.6, 1.8], Charts.colors.indigo)) +
        '</div>');

      /* 左列 */
      var left = [];
      left.push(card('估值区间：各方法并轨（Football Field）', 'scale',
        '<div class="chart">' + Charts.bandChart(band.bands || [], band.price || s.price) + '</div>' +
        (band.warnings && band.warnings.length
          ? '<div class="tbl-note">' + band.warnings.map(function (w) { return Icons.svg('info') + ' ' + esc(w); }).join('<br>') + '</div>'
          : ''),
        { right: '<span class="badge neutral">' + ((band.bands || []).length) + ' 条区间</span>' +
          evFootballField(d, band) }));

      var a = d.assumptions || {};
      var avgOf = function (arr) { return arr.reduce(function (x, y) { return x + y; }, 0) / arr.length; };
      left.push(card('核心假设与基准', 'sliders',
        '<div class="grid g-2">' +
        kvRows([
          ['基期收入', F.money(a.revenue_base)],
          ['预测期', (d.projections && d.projections.years ? (d.projections.years[0].year + ' – ' + d.projections.years[d.projections.years.length - 1].year) : '—')],
          ['收入增速（均值）', a.revenue_growth && a.revenue_growth.length ? F.pct(avgOf(a.revenue_growth)) : '—'],
          a.ebit_margin && a.ebit_margin.length
            ? ['EBIT 利润率（均值）', F.pct(avgOf(a.ebit_margin))]
            : ['毛利率 / 期间费用率',
              (F.one(a.gross_margin) != null ? F.pct(F.one(a.gross_margin)) : '—') + ' / ' +
              (F.one(a.opex_pct_revenue) != null ? F.pct(F.one(a.opex_pct_revenue)) : '—')]
        ]) +
        kvRows([
          ['WACC', F.pct(d.wacc || s.wacc)],
          ['永续增长率 g', d.terminal ? F.pct(d.terminal.terminal_growth) : '—'],
          ['所得税率', a.tax_rate != null ? F.pct(a.tax_rate) : '—'],
          ['摊薄股数', d.market ? F.num(d.market.diluted_shares / 1e8, 2) + ' 亿股' : '—']
        ]) + '</div>',
        { right: '<button class="btn sm ghost" data-go="new-run" type="button">以此为基础新建</button>' }));

      var ledger = d.evidence_ledger || {};
      var keys = Object.keys(ledger);
      if (keys.length) {
        left.push(card('假设依据与合理性审查', 'book',
          '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr><th style="width:190px">参数</th><th>依据与状态</th></tr></thead><tbody>' +
          keys.map(function (k) {
            var txt = String(ledger[k]);
            var st = /⚠️/.test(txt) ? (txt.indexOf('待替换') > -1 ? ['warn', '待替换'] : ['warn', '待查']) : ['pass', '已确认'];
            return '<tr><td class="mono" data-label="参数">' + esc(k) + '</td>' +
              '<td data-label="依据">' + esc(txt.replace(/^[^；]*；/, '')) + ' ' +
              '<span class="badge ' + st[0] + '">' + st[1] + '</span></td></tr>';
          }).join('') + '</tbody></table></div>',
          { right: '<span class="badge warn">' + keys.filter(function (k) { return /⚠️/.test(ledger[k]); }).length + ' 项待核实</span>' }));
      }

      /* 右列 */
      var right = [];
      right.push(card('可复核性', 'fingerprint',
        kvRows([
          ['运行编号', '<span class="mono">' + esc(d.run_id) + '</span>'],
          ['输入文件', '<b>' + (d.inputs || []).length + '</b> 个'],
          ['产物文件', '<b>' + (d.artifacts || []).length + '</b> 个'],
          ['执行阶段', '<b>' + (d.steps || []).length + '</b> 步'],
          ['代码版本', d.manifest && d.manifest.inputs && d.manifest.inputs.code
            ? '<span class="mono">' + esc(F.short(d.manifest.inputs.code.commit, 7)) + '</span>' +
              (d.manifest.inputs.code.dirty ? ' <span class="badge warn">dirty</span>' : '')
            : '—']
        ]) +
        '<button class="btn ai mt12" data-go="evidence" type="button">' + Icons.svg('fingerprint') + '查看复现凭证</button>', {}));

      right.push(card('数据质量', 'shield',
        (vsum ? '<div class="grid g-2" style="gap:8px">' +
          kpi('通过', String(vsum.pass), '项', '', {}) +
          kpi('告警', String(vsum.warning), '项', '', {}) +
          kpi('失败', String(vsum.fail), '项', '', {}) +
          kpi('跳过', String(vsum.skipped), '项', '<span class="muted">不等于通过</span>', {}) + '</div>'
          : '<div class="muted small">本次未产出校验报告</div>') +
        '<button class="btn ghost mt12" data-go="validation" type="button">查看逐年逐条结论</button>', {}));

      var nw = nv && nv.number_warnings ? nv.number_warnings.length : 0;
      right.push(card('报告与模型记录', 'file',
        kvRows([
          ['报告章节', nv && nv.sections ? '<b>' + nv.sections.length + '</b> 节' : '—'],
          ['叙述模型', nv ? '<span class="mono">' + esc(nv.model || '未调用') + '</span>' : '未调用'],
          ['提示词版本', nv ? '<span class="mono">' + esc(nv.prompt_version || '—') + '</span>' : '—'],
          ['token 用量', nv && nv.usage ? F.int(nv.usage.total_tokens) : '—'],
          ['数字校验', nw === 0 ? '<span class="badge pass">未发现事实表外数字</span>' : '<span class="badge warn">' + nw + ' 处待核</span>']
        ]) +
        '<button class="btn ghost mt12" data-go="report" type="button">打开报告</button>', { aiIcon: true }));

      out.push('<div class="grid g-8-4 mt14">' +
        '<div class="grid" style="gap:14px">' + left.join('') + '</div>' +
        '<div class="grid" style="gap:14px">' + right.join('') + '</div>' +
        '</div>');

      /* 底部三张分析图 */
      var tor = (d.tornado || []).slice(0, 5);
      var torItems = tor.map(function (r) {
        return { label: r.name, value: r.swing, text: F.price(r.swing) + ' 元', color: Charts.colors.indigo };
      });
      var years = (d.projections && d.projections.years) || [];
      var trend = Charts.lineChart([
        { name: '营业收入', color: Charts.colors.mint, points: years.map(function (r) { return r.income.revenue / 1e8; }), area: true },
        { name: 'EBIT', color: Charts.colors.indigo, points: years.map(function (r) { return r.income.ebit / 1e8; }) },
        { name: 'FCFF', color: Charts.colors.sky, points: (d.valuation && d.valuation.projection ? d.valuation.projection.fcff : []).map(function (x) { return x / 1e8; }) }
      ], years.map(function (r) { return r.year; }), { height: 232, fmt: function (v) { return v.toFixed(0); }, title: '三表预测趋势（亿元）' });

      var vReports = (d.validation && d.validation.reports) || [];
      var vItems = [
        { label: '通过', value: vReports.reduce(function (s2, r) { return s2 + r.summary.pass; }, 0), color: Charts.colors.mint },
        { label: '告警', value: vReports.reduce(function (s2, r) { return s2 + r.summary.warning; }, 0), color: Charts.colors.amber },
        { label: '失败', value: vReports.reduce(function (s2, r) { return s2 + r.summary.fail; }, 0), color: Charts.colors.red },
        { label: '跳过', value: vReports.reduce(function (s2, r) { return s2 + r.summary.skipped; }, 0), color: '#6B7280' }
      ];

      out.push('<div class="grid g-3 mt14">' +
        card('敏感性：Top 5 影响因子', 'trend', torItems.length
          ? '<div class="chart">' + Charts.barChart(torItems, { padL: 108, title: '单因素敏感性' }) + '</div>'
          : '<div class="tbl-note">本次未产出敏感性数据。</div>',
          { right: evTornado(d, tor[0]) + '<button class="btn sm ghost" data-go="sensitivity" type="button">全部</button>' }) +
        card('三表预测趋势（亿元）', 'layers', trend
          ? '<div class="chart">' + trend + '</div><div class="chart-legend">' +
            '<span><i style="background:' + Charts.colors.mint + '"></i>营业收入</span>' +
            '<span><i style="background:' + Charts.colors.indigo + '"></i>EBIT</span>' +
            '<span><i style="background:' + Charts.colors.sky + '"></i>FCFF</span></div>' : '') +
        card('勾稽校验总览', 'shield',
          '<div class="chart">' + Charts.barChart(vItems, { padL: 76, title: '校验结论分布' }) + '</div>' +
          '<div class="tbl-note">共 ' + vReports.reduce(function (s2, r) { return s2 + r.summary.total; }, 0) +
          ' 项检查；跳过项表示输入缺失、无法判断。</div>') +
        '</div>');

      /* 真实运行的六张 PNG 图：缩略图 + 灯箱（PRD FE-A08 / M01-06）。
         演示模式下没有产物文件，这一块不出现，图表由上面的 SVG 承担。 */
      var chartArts = (d.artifacts || []).filter(function (x) { return /\.png$/i.test(x.file); });
      if (!d.demo && chartArts.length) {
        out.push(card('分析图表（点击放大）', 'folder',
          '<div class="thumbs">' + chartArts.map(function (a) {
            var url = API.fileUrl(d.run_id, a.file);
            return '<figure class="thumb" data-img="' + esc(a.file) + '" data-img-url="' + esc(url) +
              '" tabindex="0" role="button" aria-label="放大 ' + esc(a.file) + '">' +
              '<img src="' + esc(url) + '" alt="' + esc(a.file) + '" loading="lazy">' +
              '<figcaption>' + esc(a.file.split('/').pop()) + '</figcaption></figure>';
          }).join('') + '</div>',
          { right: '<span class="badge neutral">' + chartArts.length + ' 张</span>' }));
      }

      return out.join('');
    },
    mount: function (root, c) {
      root.querySelectorAll('.thumb').forEach(function (f) {
        var open = function () { c.lightbox(f.getAttribute('data-img'), f.getAttribute('data-img-url')); };
        f.addEventListener('click', open);
        f.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
      });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 2：新建估值
     ══════════════════════════════════════════════════════════════════ */
  var newRun = {
    id: 'new-run', nav: '新建估值', icon: 'plus', group: '估值驾驶舱',
    title: '新建估值', sub: function () { return '确认标的与假设，触发一次完整估值流水线'; },
    render: function (c) {
      var d = c.data, a = d.assumptions || {}, t = d.terminal || {}, wp = d.wacc_parts || {};
      var m = d.market || {};
      var e = d.evidence_ledger || {};

      function field(label, key, val, hint, step) {
        return '<div class="field"><label>' + esc(label) +
          (hint ? '<span class="hint">' + esc(hint) + '</span>' : '') + '</label>' +
          '<input class="input" type="text" inputmode="decimal" data-param="' + esc(key) + '" value="' +
          esc(val) + '"' + (step ? ' data-step="' + esc(step) + '"' : '') + '></div>';
      }
      function seq(arr, key) {
        return (arr || []).map(function (v, i) {
          return '<input class="input" type="text" inputmode="decimal" data-param="' + esc(key) + '[' + i +
            ']" value="' + (v * 100).toFixed(1) + '" style="width:100%">';
        }).join('');
      }

      var left = [];
      left.push('<div class="fieldset"><div class="legend"><span class="idx">01</span>标的与数据来源' +
        '<span class="sub">对应 [company]</span></div>' +
        '<div class="field-row three">' +
        field('公司名称', 'company.name', d.company.name, '', '') +
        field('证券代码', 'company.code', d.company.code, '', '') +
        field('数据来源标记', 'company.source', d.company.source || '') +
        '</div>' +
        '<div class="field-row three mt12">' +
        field('当前股价（元）', 'market.price', m.price, '基准日 ' + esc(d.as_of || '')) +
        field('摊薄股数（亿股）', 'market.shares_yi', m.diluted_shares ? (m.diluted_shares / 1e8) : '', '股数按股存储') +
        field('基准日', 'market.as_of', d.as_of) +
        '</div>' +
        '<div class="row wrap mt12"><span class="chip"><span class="k">输入来源</span><b>' +
        esc(d.demo ? '内置演示样本' : (d.company.source || '本地配置文件')) + '</b></span>' +
        '<span class="chip"><span class="k">年报抽取</span>需要 pdfplumber（未接入则置灰）</span></div>' +
        '</div>');

      left.push('<div class="fieldset mt14"><div class="legend"><span class="idx">02</span>核心驱动假设（单位：%）' +
        '<span class="sub">对应 [drivers]</span></div>' +
        '<div class="field-row three">' +
        field('基期收入（亿元）', 'drivers.revenue_base_yi', a.revenue_base ? a.revenue_base / 1e8 : '', '') +
        field('基期年份', 'drivers.base_year', d.projections && d.projections.years ? d.projections.years[0].year - 1 : '', '') +
        field('预测年数', 'drivers.years', a.years || (a.revenue_growth ? a.revenue_growth.length : ''), '须与增速序列等长') +
        '</div>' +
        '<div class="field mt12"><label>逐年收入增速 <span class="hint">长度必须等于预测年数</span></label>' +
        '<div class="row" style="gap:8px">' + seq(a.revenue_growth, 'drivers.revenue_growth') + '</div></div>' +
        '<div class="field mt12"><label>逐年 EBIT 利润率 <span class="hint">长度必须等于预测年数</span></label>' +
        ((a.ebit_margin && a.ebit_margin.length)
          ? '<div class="row" style="gap:8px">' + seq(a.ebit_margin, 'drivers.ebit_margin') + '</div>'
          : '<div class="muted small">本次运行的参数快照里没有逐年 EBIT 利润率' +
            '（磁盘配置用毛利率 − 期间费用率推导），此处不做推测。</div>') + '</div>' +
        '<div class="field-row four mt12">' +
        field('毛利率 %', 'drivers.gross_margin', F.one(a.gross_margin) != null ? F.one(a.gross_margin) * 100 : '') +
        field('期间费用率 %', 'drivers.opex_pct_revenue', F.one(a.opex_pct_revenue) != null ? F.one(a.opex_pct_revenue) * 100 : '') +
        field('折旧摊销/收入 %', 'drivers.da_pct_revenue', F.one(a.da_pct_revenue) != null ? F.one(a.da_pct_revenue) * 100 : '') +
        field('资本开支/收入 %', 'drivers.capex_pct_revenue', F.one(a.capex_pct_revenue) != null ? F.one(a.capex_pct_revenue) * 100 : '') +
        '</div></div>');

      left.push('<div class="fieldset mt14"><div class="legend"><span class="idx">03</span>WACC 与终值' +
        '<span class="sub">对应 [wacc] / [terminal]</span></div>' +
        '<div class="field-row four">' +
        field('无风险利率 %', 'wacc.risk_free', wp.risk_free != null ? wp.risk_free * 100 : '') +
        field('beta', 'wacc.beta', wp.beta) +
        field('股权风险溢价 %', 'wacc.equity_risk_premium', wp.equity_risk_premium != null ? wp.equity_risk_premium * 100 : '') +
        field('债务成本 %', 'wacc.cost_of_debt', wp.cost_of_debt != null ? wp.cost_of_debt * 100 : '') +
        '</div>' +
        '<div class="field-row four mt12">' +
        field('所得税率 %', 'drivers.tax_rate', F.one(a.tax_rate) != null ? F.one(a.tax_rate) * 100 : '') +
        field('股权权重 %', 'wacc.equity_weight', wp.equity_weight != null ? wp.equity_weight * 100 : '') +
        field('永续增长率 %', 'terminal.terminal_growth', t.terminal_growth != null ? t.terminal_growth * 100 : '') +
        field('退出倍数 ×', 'terminal.exit_multiple', t.exit_multiple) +
        '</div>' +
        '<div class="field-row three mt12">' +
        '<div class="field"><label>终值方法</label><select class="input" data-param="terminal.terminal_method">' +
        '<option value="perpetuity"' + (t.terminal_method === 'perpetuity' ? ' selected' : '') + '>永续增长法</option>' +
        '<option value="exit_multiple"' + (t.terminal_method === 'exit_multiple' ? ' selected' : '') + '>退出倍数法</option>' +
        '</select></div>' +
        field('WACC 浮动幅度 %', 'sensitivity.wacc_delta', d.assumptions && c.data.sensitivity ? c.data.sensitivity.wacc_delta * 100 : 1) +
        field('网格点数', 'sensitivity.grid_points', c.data.sensitivity ? c.data.sensitivity.grid_points : 5) +
        '</div></div>');

      /* ── 04 期初资产负债表（PRD M02-08 / FE-B04）────────────────────────
         这三个恒等式必须成立，否则 build_forecast 会直接拒绝运行：
             资产 = 负债 + 权益
             资产 = 现金 + 应收 + 存货 + 固定资产 + 其他非流动资产
             负债 = 应付 + 有息负债 + 其他非流动负债
             权益 = 股本 + 留存收益 + 少数股东权益
         单位跟着数量级走：小样本用元，真实公司用亿元。 */
      var ob = d.opening_balance;
      var obGroups = [
        ['资产', [['现金', 'cash'], ['应收账款', 'accounts_receivable'], ['存货', 'inventory'],
                  ['固定资产', 'fixed_assets'], ['其他非流动资产', 'other_non_current_assets']]],
        ['负债', [['应付账款', 'accounts_payable'], ['有息负债', 'interest_bearing_debt'],
                  ['其他非流动负债', 'other_non_current_liabilities']]],
        ['权益', [['股本', 'share_capital'], ['留存收益', 'retained_earnings'],
                  ['少数股东权益', 'minority_equity']]]
      ];
      var obScale = 1, obUnit = '元';
      if (ob) {
        var obAssets = (ob.cash || 0) + (ob.accounts_receivable || 0) + (ob.inventory || 0) +
          (ob.fixed_assets || 0) + (ob.other_non_current_assets || 0);
        if (Math.abs(obAssets) >= 1e8) { obScale = 1e8; obUnit = '亿元'; }
      }
      if (ob) {
        var obHtml = obGroups.map(function (grp) {
          var cols = Math.min(4, grp[1].length);
          return '<div class="field mt12"><label>' + esc(grp[0]) + '</label>' +
            '<div class="field-row" style="grid-template-columns:repeat(' + cols + ',minmax(0,1fr))">' +
            grp[1].map(function (f) {
              var v = ob[f[1]];
              var shown = (v == null) ? '' : (v / obScale).toFixed(2);
              return '<div class="field"><label>' + esc(f[0]) + '</label>' +
                '<input class="input" type="text" inputmode="decimal" data-bs="1" ' +
                'data-param="opening_balance.' + f[1] + '" value="' + esc(shown) + '"></div>';
            }).join('') + '</div></div>';
        }).join('');
        left.push('<div class="fieldset mt14"><div class="legend"><span class="idx">04</span>期初资产负债表' +
          '<span class="sub">对应 [opening_balance] · 单位：' + obUnit + '</span></div>' + obHtml +
          '<div id="bsBalance" class="mt12" data-scale="' + obScale + '" data-unit="' + esc(obUnit) + '"></div>' +
          '<div class="row wrap mt8">' +
          '<button class="btn sm ghost" id="copyBalance" type="button">' + Icons.svg('copy') +
          '复制为 TOML 片段</button>' +
          '<span class="muted small">粘回 data/assumptions.toml 的 [opening_balance] 即可生效</span></div>' +
          '<div class="tbl-note">四个恒等式都必须成立，否则流水线会直接拒绝运行。' +
          '这里的合计是实时算的，改任何一个数都会立刻重算差额。</div></div>');
      } else {
        left.push('<div class="fieldset mt14"><div class="legend"><span class="idx">04</span>期初资产负债表' +
          '<span class="sub">对应 [opening_balance]</span></div>' +
          '<div class="muted small">本次运行的参数快照里没有期初资产负债表，' +
          '无法在这里编辑（运行一次估值后就会有）。</div></div>');
      }

      var right = [];
      right.push(card('一致性预检', 'listChecks',
        '<div id="precheck" class="list"></div>' +
        '<div class="tbl-note">预检只判断参数是否自洽（区间、长度、配平），不预估估值结果。</div>', {}));

      right.push(card('本次与基准的差异', 'activity',
        '<div id="diffBox" class="muted small">尚未修改任何参数。</div>', {}));

      right.push(card('触发运行', 'rocket',
        '<div class="grid" style="gap:9px">' +
        '<label class="row small"><input type="checkbox" id="optLlm"> 调用大模型撰写报告正文 <span class="muted">（约 2 分钟）</span></label>' +
        '<label class="row small"><input type="checkbox" id="optCharts" checked> 渲染图表（需要 matplotlib）</label>' +
        '<label class="row small"><input type="checkbox" id="optEv" checked> 产出复现凭证</label>' +
        '</div>' +
        '<button class="btn primary mt14" id="submitRun" data-param-submit="1" type="button" style="width:100%;justify-content:center">' +
        Icons.svg('play') + '运行估值</button>' +
        '<div class="tbl-note">运行会写入新的 runs/&lt;编号&gt;/ 目录，不覆盖历史结果。' +
        '当前服务端暂未开放「参数覆盖运行」接口，本次将以磁盘上的配置为准。</div>', {}));

      var out = '<div class="grid g-8-4">' +
        '<div class="grid" style="gap:14px" id="newRunForm">' + left.join('') + '</div>' +
        '<div class="grid" style="gap:14px">' + right.join('') + '</div></div>';
      return (d.demo ? demoBanner(d) : '') + out;
    },
    mount: function (root, c) {
      var base = c.data.assumptions || {}, baseM = c.data.market || {};
      var baseOb = c.data.opening_balance || null;
      var bsPanel = root.querySelector('#bsBalance');
      var obScale = bsPanel ? (Number(bsPanel.getAttribute('data-scale')) || 1) : 1;
      var obUnit = bsPanel ? (bsPanel.getAttribute('data-unit') || '元') : '元';

      var OB_KEYS = ['cash', 'accounts_receivable', 'inventory', 'fixed_assets',
        'other_non_current_assets', 'accounts_payable', 'interest_bearing_debt',
        'other_non_current_liabilities', 'share_capital', 'retained_earnings',
        'minority_equity'];

      function obValue(key) {
        var el = root.querySelector('[data-param="opening_balance.' + key + '"]');
        if (!el) return 0;
        var n = Number(String(el.value).replace(/,/g, '').trim());
        return isFinite(n) ? n * obScale : 0;      // 换回元
      }
      function balance() {
        var assets = obValue('cash') + obValue('accounts_receivable') + obValue('inventory') +
          obValue('fixed_assets') + obValue('other_non_current_assets');
        var liab = obValue('accounts_payable') + obValue('interest_bearing_debt') +
          obValue('other_non_current_liabilities');
        var equity = obValue('share_capital') + obValue('retained_earnings') + obValue('minority_equity');
        return { assets: assets, liab: liab, equity: equity, gap: assets - (liab + equity) };
      }
      function balanceOk(b) {
        return Math.abs(b.gap) <= Math.max(1, Math.abs(b.assets) * 1e-9);
      }
      function paintBalance() {
        if (!bsPanel) return;
        var b = balance();
        var ok = balanceOk(b);
        var fmt = function (x) {
          /* 必须除回显示单位：样本量级为「元」时倍率是 1，看不出问题，
             换成上亿的真实公司就会显示成 196,020,000,000 亿元。 */
          return (x / obScale).toLocaleString('zh-CN',
            { minimumFractionDigits: 2, maximumFractionDigits: 2 }) +
            ' ' + obUnit;
        };
        bsPanel.dataset.ok = ok ? '1' : '0';
        bsPanel.innerHTML = '<div class="statbar">' +
          '<span>资产合计 <b>' + fmt(b.assets) + '</b></span>' +
          '<span>负债合计 <b>' + fmt(b.liab) + '</b></span>' +
          '<span>权益合计 <b>' + fmt(b.equity) + '</b></span>' +
          '<span>负债 + 权益 <b>' + fmt(b.liab + b.equity) + '</b></span>' +
          '<span class="' + (ok ? 'delta up' : 'delta down') + '">差额 <b>' + fmt(b.gap) + '</b></span>' +
          '<span class="badge ' + (ok ? 'pass' : 'fail') + '">' + (ok ? '配平 ✓' : '不平 ✕') + '</span>' +
          '</div>';
      }

      function rules() {
        var g = [];
        var growth = root.querySelectorAll('[data-param^="drivers.revenue_growth"]');
        var margin = root.querySelectorAll('[data-param^="drivers.ebit_margin"]');
        var years = root.querySelector('[data-param="drivers.years"]');
        var tax = root.querySelector('[data-param="drivers.tax_rate"]');
        var waccG = root.querySelector('[data-param="terminal.terminal_growth"]');
        var waccIn = root.querySelector('[data-param="wacc.equity_weight"]');
        var debtIn = root.querySelector('[data-param="wacc.debt_weight"]');
        var y = Number(years && years.value);
        g.push(['增速序列长度与预测年数一致', growth.length === y, '增速 ' + growth.length + ' 项 / 年数 ' + y]);
        if (margin.length) {
          g.push(['利润率序列长度与预测年数一致', margin.length === y, '利润率 ' + margin.length + ' 项']);
        } else {
          g.push(['利润率序列长度与预测年数一致', true, '本次未登记逐年利润率，跳过该检查']);
        }
        var tv = Number(tax && tax.value);
        g.push(['所得税率在 0–60% 之间', isFinite(tv) && tv >= 0 && tv <= 60, tv + '%']);
        var gv = Number(waccG && waccG.value);
        var wv = c.data.wacc != null ? c.data.wacc * 100 : null;
        g.push(['永续增长率小于 WACC（终值收敛）', gv < (wv || 8), 'g=' + gv + '% / WACC≈' + (wv ? wv.toFixed(2) : '—') + '%']);
        g.push(['所有数值均为有限数', true, '无 NaN / 无穷大']);
        if (bsPanel) {
          var b = balance();
          var ok = balanceOk(b);
          g.push(['期初资产负债表配平（资产 = 负债 + 权益）', ok,
            '资产 ' + (b.assets / obScale).toFixed(2) + ' / 负债+权益 ' +
            ((b.liab + b.equity) / obScale).toFixed(2) + ' ' + obUnit +
            (ok ? '' : '，差额 ' + (b.gap / obScale).toFixed(2))]);
        }
        return g;
      }
      function paint() {
        var precheck = root.querySelector('#precheck');
        var diffBox = root.querySelector('#diffBox');
        if (!precheck || !diffBox) return;   // 已经离开这个页面
        paintBalance();                       // 先算合计，预检要用到差额
        var rs = rules();
        precheck.innerHTML = rs.map(function (r) {
          return '<div class="list-item"><span class="badge ' + (r[1] ? 'pass' : 'fail') + '">' +
            (r[1] ? '✓' : '✕') + '</span><div class="li-main">' + esc(r[0]) +
            '<div class="li-sub">' + esc(r[2]) + '</div></div></div>';
        }).join('');
        var changed = 0, details = [];
        root.querySelectorAll('[data-param]').forEach(function (el) {
          var key = el.getAttribute('data-param');
          if (/^company\./.test(key) || /^market\./.test(key) || /^terminal\./.test(key) && key.indexOf('method') > -1) return;
          var cur = Number(String(el.value).replace(/,/g, ''));
          if (!isFinite(cur)) return;
          var orig = null;
          if (key === 'drivers.revenue_base_yi') orig = base.revenue_base / 1e8;
          else if (key === 'drivers.tax_rate') orig = F.one(base.tax_rate) * 100;
          else if (key === 'drivers.gross_margin') orig = F.one(base.gross_margin) * 100;
          else if (key === 'drivers.opex_pct_revenue') orig = F.one(base.opex_pct_revenue) * 100;
          else if (key === 'drivers.da_pct_revenue') orig = F.one(base.da_pct_revenue) * 100;
          else if (key === 'drivers.capex_pct_revenue') orig = F.one(base.capex_pct_revenue) * 100;
          else if (key.slice(0, 8) === 'drivers.') orig = null;
          else if (key === 'market.price') orig = baseM.price;
          else if (key === 'market.shares_yi') orig = baseM.diluted_shares / 1e8;
          else if (key === 'terminal.terminal_growth') orig = (c.data.terminal || {}).terminal_growth * 100;
          else if (key === 'terminal.exit_multiple') orig = (c.data.terminal || {}).exit_multiple;
          else if (key.indexOf('opening_balance.') === 0) {
            var of = key.slice('opening_balance.'.length);
            orig = (baseOb && baseOb[of] != null) ? baseOb[of] / obScale : null;
          }
          if (orig != null && Math.abs(cur - orig) > 1e-6) {
            changed++;
            details.push(key + '：' + orig + ' → ' + cur);
          }
        });
        diffBox.innerHTML = changed
          ? '<b>' + changed + '</b> 项参数已被修改：<div class="li-sub" style="margin-top:6px">' +
            details.map(esc).join('<br>') + '</div>'
          : '尚未修改任何参数。';
      }
      /* 监听挂在表单容器上，绝不能挂在 #view 上：#view 跨页面复用，
         挂在它上面等于每进一次这个页面就多挂一个监听，离开后再输入
         就会打到已经不存在的 #precheck / #diffBox 上。 */
      var form = root.querySelector('#newRunForm');
      if (form) {
        form.addEventListener('input', paint);
        form.addEventListener('change', paint);
      }
      paint();

      var btn = root.querySelector('#submitRun');
      btn.addEventListener('click', function () {
        c.triggerRun({
          useLlm: root.querySelector('#optLlm').checked,
          charts: root.querySelector('#optCharts').checked
        });
      });

      /* 把当前这套期初资产负债表导出成可直接粘贴的 TOML 片段。
         后端还没有参数化运行接口（API-05），这一步是现阶段真正可用的落地方式。 */
      var copyBtn = root.querySelector('#copyBalance');
      if (copyBtn) {
        copyBtn.addEventListener('click', function () {
          var lines = ['[opening_balance]'];
          OB_KEYS.forEach(function (k) {
            lines.push(k + ' = ' + obValue(k).toFixed(2));
          });
          c.copy(lines.join('\n'));
        });
      }
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 3：运行监控
     ══════════════════════════════════════════════════════════════════ */
  var runConsole = {
    id: 'run', nav: '运行监控', icon: 'activity', group: '估值驾驶舱',
    title: '运行监控', sub: function (c) { return '运行编号 ' + c.data.run_id; },
    render: function (c) {
      var d = c.data, st = c.status || {};
      var steps = d.steps || [];
      var total = steps.reduce(function (s, x) { return s + (x.duration_s || 0); }, 0);
      var done = steps.filter(function (s) { return s.status === 'ok'; }).length;
      var running = !!st.running;
      var pct = steps.length ? Math.round(done / steps.length * 100) : 0;
      var nv = narrativeOf(d);
      var out = [];
      out.push(demoBanner(d));

      out.push(card('', null,
        '<div class="row wrap" style="gap:16px">' +
        '<div><div class="kpi-label">' + Icons.svg('activity') + '流水线状态</div>' +
        '<div style="font-size:19px;font-weight:600">' + (running ? '正在执行' : (st.error ? '执行失败' : '运行完成')) + '</div>' +
        '<div class="small muted">' + esc(F.dt(d.created_at || (st.run_id ? '' : ''))) + '</div></div>' +
        '<div style="flex:1;min-width:220px"><div class="row small muted"><span>阶段进度</span>' +
        '<span class="spacer"></span><b class="mono">' + done + ' / ' + steps.length + '</b></div>' +
        '<div class="bar mt8"><i style="width:' + pct + '%"></i></div></div>' +
        '<div class="row" style="gap:10px">' +
        '<span class="chip"><span class="k">总耗时</span><b>' + F.dur(total) + '</b></span>' +
        '<span class="chip"><span class="k">产物</span><b>' + (d.artifacts || []).length + '</b></span>' +
        '<button class="btn ghost" id="rerunBtn" type="button">' + Icons.svg('refresh') + '重跑</button>' +
        '</div></div>', { cls: 'tight' }));

      var failed = steps.filter(function (x) { return x.status && x.status !== 'ok'; });
      var firstErr = (failed[0] && failed[0].extra && failed[0].extra.error) || '';
      if (failed.length || (st && st.error)) {
        out.push('<div class="banner fail" style="margin:0 0 14px">' + Icons.svg('alert') +
          '<div><b>运行失败</b>：' +
          esc((st && st.error) || (failed[0] ? ('阶段 ' + failed[0].step) : '')) +
          (firstErr ? '<div class="mono small mt8">' + esc(firstErr) + '</div>' : '') +
          '<div class="small mt8">可先「重跑」；重复失败时展开下面的执行轨迹日志看原始记录。' +
          '</div></div></div>');
      }

      out.push(card('阶段流水线（按执行轨迹渲染，不硬编码阶段名）', 'layers',
        '<div class="pipe" id="pipe">' + (steps.length ? steps.map(function (s, i) {
          var cls = s.status === 'ok' ? 'done' : (s.status === 'failed' ? 'fail' : (running && i === done ? 'run' : 'wait'));
          return '<div class="pipe-step ' + cls + '"><span class="s-name">' + esc(s.step) + '</span>' +
            '<span class="s-meta">' + F.dur(s.duration_s) + '</span>' +
            '<span class="s-meta">' + esc(s.tool || '') + '</span></div>';
        }).join('') : '<div class="muted small">还没有执行轨迹。</div>') + '</div>' +
        '<div class="tbl-note">阶段名称与数量取自 logs/&lt;运行编号&gt;/trace.jsonl 的 step 字段。</div>', {}));

      var left = [];
      left.push(card('智能投研叙述', 'sparkles',
        nv ? (nv.ok
          ? '<div class="row wrap" style="gap:8px"><span class="badge ai">' + Icons.svg('sparkles') + 'AI 生成</span>' +
            '<span class="chip"><span class="k">模型</span><b>' + esc(nv.model) + '</b></span>' +
            '<span class="chip"><span class="k">提示词</span><b>' + esc(nv.prompt_version || '—') + '</b></span>' +
            '<span class="chip"><span class="k">轮次</span><b>' + (nv.rounds || 1) + '</b></span>' +
            '<span class="chip"><span class="k">耗时</span><b>' + F.dur(nv.elapsed_s) + '</b></span></div>' +
            '<div class="grid g-3 mt12" style="gap:10px">' +
            kpi('完成章节', String((nv.sections || []).length), '节', '', {}) +
            kpi('token 用量', F.int(nv.usage && nv.usage.total_tokens), '', '', {}) +
            kpi('数字校验', (nv.number_warnings || []).length === 0 ? '通过' : '待核', '',
              '<span class="muted">正文数字与事实表比对</span>', {}) + '</div>'
          : '<div class="banner warn" style="margin:0">' + Icons.svg('alert') +
            '<div>本次未调用大模型，报告只含数据表与模板文字。</div></div>')
          : '<div class="muted small">本次运行没有叙述记录。</div>',
        { aiIcon: true, right: '<button class="btn sm ghost" data-go="report" type="button">查看报告</button>' }));

      var trace = steps.map(function (s) {
        return ['[' + F.dt(s.timestamp) + '] step=' + s.step, 'status=' + s.status,
          'duration=' + (s.duration_s != null ? s.duration_s + 's' : '—')].join(' ');
      }).join('\n');
      left.push(card('执行轨迹日志', 'terminal',
        '<pre class="code" id="traceLog">' + esc(trace || '暂无轨迹。') + '</pre>',
        { right: '<span class="badge neutral">' + steps.length + ' 行</span>' }));

      var right = [];
      var arts = d.artifacts || [];
      right.push(card('实时产物清单', 'folder',
        arts.length ? '<div class="list">' + arts.map(function (a) {
          var isChart = /\.png$/i.test(a.file);
          return '<div class="list-item"><span class="badge neutral">' + esc(a.kind || (isChart ? '图表' : '数据')) + '</span>' +
            '<div class="li-main"><div class="mono small">' + esc(a.file) + '</div>' +
            '<div class="li-sub">' + F.bytes(a.bytes) + (a.sha256 ? ' · ' + esc(F.short(a.sha256, 10)) + '…' : '') + '</div></div>' +
            '<div class="li-right">' + (d.demo ? '' : '<a class="icon-btn" href="' + API.fileUrl(d.run_id, a.file) +
            '" target="_blank" rel="noopener" title="打开" aria-label="打开">' + Icons.svg('external') + '</a>') +
            '</div></div>';
        }).join('') + '</div>' : '<div class="muted small">本次未产出文件。</div>',
        { right: '<span class="badge neutral">' + arts.length + ' 个</span>' }));

      right.push(card('本次运行信息', 'info',
        kvRows([
          ['运行编号', '<span class="mono">' + esc(d.run_id) + '</span>'],
          ['创建时间', esc(F.dt(d.created_at))],
          ['标的', esc(d.company.name || '—')],
          ['基准日', esc(d.as_of || '—')],
          ['警告', (d.warnings || []).length ? '<span class="badge warn">' + d.warnings.length + ' 条</span>' : '<span class="badge pass">无</span>']
        ]), {}));

      out.push('<div class="grid g-8-4 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    },
    mount: function (root, c) {
      var b = root.querySelector('#rerunBtn');
      if (b) b.addEventListener('click', function () { c.triggerRun({}); });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 4：估值结果
     ══════════════════════════════════════════════════════════════════ */
  var result = {
    id: 'result', nav: '估值结果', icon: 'target', group: '分析工作台',
    title: '估值结果', sub: function (c) { return 'DCF 推理链 · ' + c.data.run_id; },
    render: function (c) {
      var d = c.data, s = valSummary(d), v = d.valuation || {};
      var out = [];
      out.push(demoBanner(d));

      out.push('<div class="verdict">' +
        '<div><div class="kpi-label">' + Icons.svg('target') + '每股价值（DCF）</div>' +
        '<div class="v-score">' + F.price(s.per) + '<span class="unit" style="font-size:14px;color:var(--text-2)"> 元</span></div></div>' +
        '<div class="v-text">现价 ' + F.price(s.price) + ' 元 · WACC ' + F.pct(s.wacc, 2) +
        ' · 终值占比 ' + F.pct(s.tshare, 2) + ' · 隐含退出倍数 ' + F.x(s.exitM) + '×</div>' +
        '<div class="v-meta"><span class="badge ' + (s.ret >= 0 ? 'up' : 'down') + '">' +
        (s.ret >= 0 ? '▲' : '▼') + ' ' + F.pctSigned(s.ret) + '</span>' +
        '<span class="badge fact">' + Icons.svg('check') + '事实</span>' +
        '<span class="badge infer">' + Icons.svg('trend') + '推论</span>' +
        evPerShare(d, s) + '</div></div>');

      out.push('<div class="grid g-4 mt14">' +
        kpi('企业价值', F.moneyShort(s.ev, 2), F.moneyUnit(s.ev),
          '<span class="muted">WACC ' + F.pct(s.wacc) + '</span>' + evPerShare(d, s)) +
        kpi('权益价值', F.moneyShort(s.equity, 2), F.moneyUnit(s.equity), '<span class="muted">净债务 ' + F.money(s.netDebt) + '</span>') +
        kpi('终值现值', F.moneyShort(v.pv_terminal, 2), F.moneyUnit(v.pv_terminal), '<span class="muted">占企业价值 ' + F.pct(s.tshare) + '</span>') +
        kpi('预测期现值合计', F.moneyShort((v.pv_fcff || []).reduce(function (a, b) { return a + b; }, 0), 2),
          F.moneyUnit(s.ev), '<span class="muted">' + ((v.pv_fcff || []).length) + ' 年 FCFF 折现</span>') +
        '</div>');

      /* 估值桥 */
      var bridge = [
        { label: '企业价值', value: v.enterprise_value },
        { label: '减：净债务', value: -v.net_debt },
        { label: '权益价值', value: v.equity_value },
        { label: '每股价值', value: v.per_share }
      ];
      var bridgeSvg = Charts.bridgeChart([
        { label: '企业价值', value: v.enterprise_value / 1e8 },
        { label: '净债务调整', value: -v.net_debt / 1e8 },
        { label: '权益价值', value: v.equity_value / 1e8 }
      ]);
      var left = [];
      left.push(card('估值桥（Valuation Bridge）', 'scale',
        '<div class="chart">' + bridgeSvg + '</div>' +
        '<div class="tbl-note">单位为亿元。企业价值 −（付息债务 − 现金）= 权益价值；权益价值 ÷ 摊薄股数 = 每股价值 ' +
        F.price(v.per_share) + ' 元。</div>',
        { right: '<span class="badge neutral">' + Icons.svg('table') + '可复制</span>', cls: '' }));

      /* 预测期明细 */
      var p = v.projection || {};
      var years = p.years || [];
      var rows = years.map(function (y, i) {
        return '<tr><td class="num">' + y + '</td>' +
          '<td class="num">' + F.moneyShort(p.revenue[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.ebit[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.nopat[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.da[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.capex[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.delta_nwc[i]) + '</td>' +
          '<td class="num">' + F.moneyShort(p.fcff[i]) + '</td>' +
          '<td class="num">' + F.x((v.discount_factor || [])[i], 4) + '</td>' +
          '<td class="num">' + F.moneyShort((v.pv_fcff || [])[i]) + '</td></tr>';
      }).join('');
      left.push(card('预测期现金流明细', 'table',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        ['年份', '营业收入', 'EBIT', 'NOPAT', '折旧摊销', '资本开支', '营运资本增加', 'FCFF', '折现因子', '现值']
          .map(function (h, i) { return '<th class="' + (i ? 'num' : '') + '">' + h + (i ? '<br><span style="font-weight:400">亿元</span>' : '') + '</th>'; }).join('') +
        '</tr></thead><tbody>' + (rows || '<tr><td colspan="10" class="dim">本次未产出预测明细</td></tr>') + '</tbody></table></div>' +
        '<div class="tbl-note">金额单位为亿元（后端原始单位为元）；折现因子保留 4 位小数。</div>', {}));

      /* 右列 */
      var right = [];
      right.push(card('关键假设', 'sliders', kvRows([
        ['WACC', F.pct(d.wacc || s.wacc)],
        ['永续增长率', d.terminal ? F.pct(d.terminal.terminal_growth) : '—'],
        ['终值方法', d.terminal ? (d.terminal.terminal_method === 'perpetuity' ? '永续增长法' : '退出倍数法') : '—'],
        ['净债务', F.money(s.netDebt)],
        ['摊薄股数', d.market ? F.num(d.market.diluted_shares / 1e8, 2) + ' 亿股' : '—'],
        ['当前股价', F.price(s.price) + ' 元']
      ]), {}));
      var diag = s.diag || [];
      right.push(card('模型诊断与告警', 'alert',
        diag.length ? '<div class="list">' + diag.map(function (x) {
          return '<div class="list-item"><span class="badge warn">提示</span><div class="li-main">' + esc(x) + '</div></div>';
        }).join('') + '</div>' : '<div class="badge pass">' + Icons.svg('check') + '未发现异常诊断</div>', {}));

      var fcff = ((v.projection || {}).fcff || []).map(function (x) { return x / 1e8; });
      right.push(card('FCFF 走势', 'trend', fcff.length
        ? '<div class="chart">' + Charts.lineChart([{ name: 'FCFF', color: Charts.colors.mint, points: fcff, area: true }],
          years.map(String), { height: 180, fmt: function (x) { return x.toFixed(0); } }) + '</div>' : '',
        { right: '<span class="badge neutral">亿元</span>' }));

      out.push('<div class="grid g-8-4 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 5：敏感性分析
     ══════════════════════════════════════════════════════════════════ */
  var sensitivity = {
    id: 'sensitivity', nav: '敏感性分析', icon: 'sliders', group: '分析工作台',
    title: '敏感性分析', sub: function () { return '哪个假设最值得争论，什么组合下结论会翻转'; },
    render: function (c) {
      var d = c.data, s = valSummary(d);
      var tor = d.tornado || [], g = d.grid;
      var out = [];
      out.push(demoBanner(d));
      var widest = tor[0] || {};
      var gridVals = [];
      if (g && g.cells) g.cells.forEach(function (r) { r.forEach(function (x) { if (x != null) gridVals.push(x); }); });
      var belowCount = gridVals.filter(function (x) { return x < (g ? g.price : s.price); }).length;

      out.push('<div class="grid g-4">' +
        kpi('最大波动幅度', F.price(widest.swing), '元/股',
          '<span class="muted">来自「' + esc(widest.name || '—') + '」</span>' + evTornado(d, widest),
          { hero: true, icon: 'sliders' }) +
        kpi('基准每股价值', F.price(widest.per_share_base != null ? widest.per_share_base : s.per), '元', '<span class="muted">现价 ' + F.price(s.price) + ' 元</span>') +
        kpi('网格低于现价组合', String(belowCount), '个', '<span class="muted">共 ' + gridVals.length + ' 个组合</span>') +
        kpi('WACC / 永续增长率', F.pct(d.wacc || s.wacc, 2) + ' / ' + F.pct(d.terminal ? d.terminal.terminal_growth : null, 2), '',
          '<span class="muted">双因素网格的两个轴</span>') +
        '</div>');

      var left = [];
      left.push(card('单因素敏感性（龙卷风图）', 'trend',
        '<div class="chart">' + Charts.tornadoChart(tor, widest.per_share_base != null ? widest.per_share_base : s.per) + '</div>',
        { right: '<span class="badge neutral">按波动幅度排序</span>' }));

      left.push(card('敏感性排序表', 'table',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>变量</th><th class="num">下界</th><th class="num">基准</th><th class="num">上界</th>' +
        '<th class="num">下界每股价值</th><th class="num">上界每股价值</th><th class="num">波动幅度</th></tr></thead><tbody>' +
        (tor.length ? tor.map(function (r) {
          return '<tr><td data-label="变量">' + esc(r.name) + '</td>' +
            '<td class="num" data-label="下界">' + F.pct(r.low) + '</td>' +
            '<td class="num" data-label="基准">' + F.pct(r.base) + '</td>' +
            '<td class="num" data-label="上界">' + F.pct(r.high) + '</td>' +
            '<td class="num" data-label="下界每股价值">' + F.price(r.per_share_at_low) + '</td>' +
            '<td class="num" data-label="上界每股价值">' + F.price(r.per_share_at_high) + '</td>' +
            '<td class="num" data-label="波动幅度">' + F.price(r.swing) + '</td></tr>';
        }).join('') : '<tr><td colspan="7" class="dim">本次未产出敏感性数据</td></tr>') +
        '</tbody></table></div>', {}));

      var right = [];
      right.push(card('双因素敏感性热力网格', 'grid',
        g ? Charts.heatmapTable(g, g.price) : '<div class="tbl-note">本次未产出双因素网格。</div>',
        { right: '<span class="badge neutral">数值视图（不依赖颜色）</span>' }));

      right.push(card('结论', 'info',
        '<div class="prose" style="font-size:13px">' +
        '<p>影响最大的假设是 <b>' + esc(widest.name || '—') + '</b>：在 ' + F.pct(widest.low) + ' 到 ' +
        F.pct(widest.high) + ' 的区间内，每股价值从 ' + F.price(widest.per_share_at_low) + ' 元变化到 ' +
        F.price(widest.per_share_at_high) + ' 元，波动幅度 ' + F.price(widest.swing) + ' 元。</p>' +
        '<p>双因素网格中有 <b>' + belowCount + '</b> 个组合的每股价值低于现价 ' + F.price(g ? g.price : s.price) +
        ' 元，说明结论在这些组合下会翻转；这些区域在网格里以偏红的底色标出，并同时给出数值。</p>' +
        '<p class="muted small">本页所有数值均来自 04_tornado.csv 与 05_sensitivity.csv；网格读数以数值为准，颜色只是辅助。</p>' +
        '</div>', {}));

      out.push('<div class="grid g-7-5 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    },
    /* 网格读数：鼠标移动或键盘 focus 时，在单元格上方浮出该组合的估值（PRD FE-C07） */
    mount: function (root) {
      var table = root.querySelector('table.heat');
      if (!table) return;
      var price = Number(table.getAttribute('data-price'));
      /* 全局只留一个浮层元素：每次进这个页面都新建一个的话，
         body 里会越堆越多（旧的那些虽然隐藏着，但仍然占着 DOM）。 */
      var tip = document.querySelector('.chart-tip');
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'chart-tip';
        tip.hidden = true;
        document.body.appendChild(tip);
      }
      function show(cell) {
        var rv = Number(cell.getAttribute('data-rv'));
        var cv = Number(cell.getAttribute('data-cv'));
        var v = Number(cell.getAttribute('data-v'));
        tip.textContent = 'g ' + (rv * 100).toFixed(2) + '% ／ WACC ' + (cv * 100).toFixed(2) +
          '% → 每股价值 ' + v.toFixed(2) + ' 元' +
          (isFinite(price) && v < price ? '（低于现价 ' + price.toFixed(2) + '）' : '');
        var box = cell.getBoundingClientRect();
        tip.style.left = (box.left + box.width / 2) + 'px';
        tip.style.top = box.top + 'px';
        tip.hidden = false;
      }
      function hide() { tip.hidden = true; }
      table.addEventListener('mouseover', function (e) {
        var cell = e.target.closest('.heat-cell');
        if (cell) show(cell); else hide();
      });
      table.addEventListener('mouseleave', hide);
      table.addEventListener('focusin', function (e) {
        var cell = e.target.closest('.heat-cell');
        if (cell) show(cell);
      });
      table.addEventListener('focusout', hide);
      /* 滚动时提示会错位，所以滚动就收起。监听挂在 window 上，
         但带回自清理：表格已经不在文档里时就把监听摘掉，
         避免像 input 监听那样跨页面越挂越多。 */
      var onScroll = function () {
        if (!document.body.contains(table)) {
          window.removeEventListener('scroll', onScroll, true);
        }
        hide();
      };
      window.addEventListener('scroll', onScroll, true);
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 6：反向估值
     ══════════════════════════════════════════════════════════════════ */
  var reverse = {
    id: 'reverse', nav: '反向估值', sub: function () { return '现价隐含了哪些假设'; },
    icon: 'refresh', group: '分析工作台',
    title: '反向估值',
    render: function (c) {
      var d = c.data, s = valSummary(d);
      var rs = (d.reverse && d.reverse.results) || [];
      var ok = rs.filter(function (r) { return !r.skipped && r.implied_value != null; });
      var out = [];
      out.push(demoBanner(d));

      out.push('<div class="grid g-4">' +
        kpi('目标价（现价）', F.price(rs.length ? rs[0].target_per_share : s.price), '元', '<span class="muted">反解的目标</span>', { icon: 'target' }) +
        kpi('DCF 每股价值', F.price(s.per), '元', '<span class="badge ' + (s.ret >= 0 ? 'up' : 'down') + '">' + F.pctSigned(s.ret) + '</span>', { hero: true }) +
        kpi('成功反解变量', String(ok.length) + ' / ' + rs.length, '', '<span class="muted">其余因区间不收敛被跳过</span>') +
        kpi('最大差距', ok.length ? (function () {
          var mx = ok[0];
          ok.forEach(function (r) {
            if (Math.abs(r.implied_value - r.base_value) > Math.abs(mx.implied_value - mx.base_value)) mx = r;
          });
          return F.pct(mx.implied_value - mx.base_value);
        })() : '—', '', '<span class="muted">市场隐含值 − 我们的取值</span>') +
        '</div>');

      var rows = rs.map(function (r) {
        var dir = r.direction === 'higher' ? ['up', '市场更高 ▲'] : r.direction === 'lower' ? ['down', '市场更低 ▼'] : ['neutral', '基本一致'];
        return '<tr><td data-label="变量">' + esc(r.name) + '</td>' +
          '<td class="mono" data-label="字段">' + esc(r.field) + '</td>' +
          '<td class="num" data-label="我们的取值">' + F.pct(r.base_value) + '</td>' +
          '<td class="num" data-label="市场隐含">' + (r.skipped ? '—' : F.pct(r.implied_value)) + '</td>' +
          '<td data-label="方向">' + (r.skipped ? '<span class="badge skip">未收敛</span>' : '<span class="badge ' + dir[0] + '">' + dir[1] + '</span>') + '</td></tr>';
      }).join('');

      var left = [];
      left.push(card('市场隐含假设（Implied Expectations）', 'refresh',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>变量</th><th>字段</th><th class="num">我们的取值</th><th class="num">市场隐含</th><th>方向</th></tr></thead>' +
        '<tbody>' + (rows || '<tr><td colspan="5" class="dim">本次未产出反向求解结果</td></tr>') + '</tbody></table></div>',
        { right: evReverse(d, ok[0] || rs[0]) }));

      var barItems = ok.map(function (r) {
        return { label: r.name, value: Math.abs(r.implied_value - r.base_value) * 1000 / 10,
          text: F.pctSigned(r.implied_value - r.base_value),
          color: r.direction === 'higher' ? Charts.colors.mint : Charts.colors.amber };
      });
      left.push(card('与基准的差距', 'trend', barItems.length
        ? '<div class="chart">' + Charts.barChart(barItems, { padL: 116, title: '隐含值与基准的差距' }) + '</div>' +
          '<div class="tbl-note">柱长表示差距绝对值，右侧文字给出方向与幅度。</div>'
        : '<div class="tbl-note">没有可比较的反解结果。</div>', {}));

      var right = [];
      right.push(card('这一步在问什么', 'info',
        '<div class="prose" style="font-size:13px">' +
        '<p>反向估值把当前股价当作给定条件，反过来求解：<b>市场需要相信什么假设，才会给出这个价格？</b></p>' +
        '<p>做法是把每个变量单独调整，使 DCF 算出的每股价值等于现价，其余变量保持基准值不变。' +
        '得到的结果是「孤立来看」的隐含值，不能理解为市场真的这么认为。</p>' +
        '<p class="muted small">反解结果来自 06_reverse_dcf.json。</p></div>', {}));

      var skipped = rs.filter(function (r) { return r.skipped; });
      var note = (d.notes && d.notes.reverse_skipped) || [];
      right.push(card('未能反解的变量', 'alert',
        (skipped.length || note.length)
          ? '<div class="list">' + skipped.map(function (r) {
              return '<div class="list-item"><span class="badge skip">跳过</span><div class="li-main">' +
                esc(r.name) + '<div class="li-sub">在该变量的搜索区间内无法使每股价值等于目标价</div></div></div>';
            }).join('') + note.map(function (n) {
              return '<div class="list-item"><span class="badge warn">说明</span><div class="li-main">' + esc(n) + '</div></div>';
            }).join('') + '</div>'
          : '<div class="badge pass">' + Icons.svg('check') + '全部变量均成功反解</div>', {}));

      out.push('<div class="grid g-7-5 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    }
  };

  global.Pages = {
    list: [dashboard, newRun, runConsole, result, sensitivity, reverse],
    F: F, card: card, kpi: kpi, kvRows: kvRows, hashChip: hashChip,
    demoBanner: demoBanner, emptyBlock: emptyBlock, statusBadge: statusBadge, valSummary: valSummary,
    evBtn: evBtn, evPerShare: evPerShare, evFootballField: evFootballField,
    evTornado: evTornado, evReverse: evReverse, evCompsImplied: evCompsImplied
  };
})(window);
