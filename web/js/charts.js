/* 图表层：手写 SVG，不依赖任何图表库。
   全部图表都从产物数据出，颜色只表达状态，不单独承载含义
   （每个数值都同时有文字标注，满足「不依赖颜色」的要求）。 */
(function (global) {
  'use strict';

  var C = {
    mint: '#00E599', mintDim: '#0B8F63', indigo: '#6366F1', violet: '#8B5CF6',
    amber: '#F59E0B', red: '#EF4444', sky: '#38BDF8',
    grid: 'rgba(255,255,255,.07)', axis: '#606877', text: '#9CA3AF', strong: '#F3F4F6'
  };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  };
  var n2 = function (x) { return (Math.round(x * 100) / 100).toFixed(2); };

  function ticks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + 1; }
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = span / count / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var out = [], v = Math.ceil(lo / step) * step;
    for (; v <= hi + step * 0.5; v += step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  }

  /* ── 1. 估值区间（足球场图） ───────────────────────────────────────── */
  function bandChart(bands, price, opts) {
    opts = opts || {};
    if (!bands || !bands.length) return '<div class="tbl-note">本次运行未产出估值区间。</div>';
    var W = 880, rowH = 34, padL = 150, padR = 84, padT = 26, padB = 34;
    var H = padT + bands.length * rowH + padB;
    var lows = bands.map(function (b) { return Math.min(b.low, b.high); });
    var highs = bands.map(function (b) { return Math.max(b.low, b.high); });
    lows.push(price); highs.push(price);
    var lo = Math.min.apply(null, lows), hi = Math.max.apply(null, highs);
    var pad = (hi - lo) * 0.1 || 1;
    lo -= pad; hi += pad;
    var x = function (v) { return padL + (v - lo) / (hi - lo) * (W - padL - padR); };

    var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="各方法的估值区间与现价位置">'];
    ticks(lo, hi, 5).forEach(function (t) {
      s.push('<line x1="' + n2(x(t)) + '" y1="' + (padT - 10) + '" x2="' + n2(x(t)) + '" y2="' + (H - padB + 4) +
        '" stroke="' + C.grid + '"/>');
      s.push('<text x="' + n2(x(t)) + '" y="' + (H - padB + 20) + '" fill="' + C.axis +
        '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">' + n2(t) + '</text>');
    });

    bands.forEach(function (b, i) {
      var y = padT + i * rowH + rowH / 2;
      var x1 = x(Math.min(b.low, b.high)), x2 = x(Math.max(b.low, b.high));
      var xc = x(b.central == null ? (b.low + b.high) / 2 : b.central);
      var color = i === 0 ? C.mint : C.indigo;
      var below = price > Math.max(b.low, b.high);
      s.push('<text x="' + (padL - 12) + '" y="' + (y + 3.5) + '" fill="' + C.text +
        '" font-size="11.5" text-anchor="end">' + esc(b.name) + '</text>');
      s.push('<rect x="' + n2(x1) + '" y="' + (y - 8) + '" width="' + n2(Math.max(3, x2 - x1)) +
        '" height="16" rx="8" fill="' + color + '" opacity="' + (i === 0 ? '.30' : '.20') +
        '" stroke="' + color + '" stroke-opacity=".5"/>');
      s.push('<line x1="' + n2(xc) + '" y1="' + (y - 11) + '" x2="' + n2(xc) + '" y2="' + (y + 11) +
        '" stroke="' + color + '" stroke-width="2"/>');
      s.push('<text x="' + (W - padR + 10) + '" y="' + (y + 3.5) + '" fill="' + C.strong +
        '" font-size="11.5" font-family="JetBrains Mono, Consolas, monospace">' + n2(b.central) + '</text>');
      s.push('<text x="' + (padL - 12 + 2) + '" y="' + (y + 16) + '" fill="' + C.axis +
        '" font-size="9.5" text-anchor="end">' + (below ? '低于现价' : '') + '</text>');
    });

    var xp = x(price);
    s.push('<line x1="' + n2(xp) + '" y1="' + (padT - 14) + '" x2="' + n2(xp) + '" y2="' + (H - padB + 6) +
      '" stroke="' + C.red + '" stroke-width="1.4" stroke-dasharray="5 4"/>');
    s.push('<text x="' + n2(xp) + '" y="' + (padT - 18) + '" fill="' + C.red +
      '" font-size="11" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">现价 ' + n2(price) + '</text>');
    s.push('</svg>');
    return s.join('');
  }

  /* ── 2. 龙卷风图 ───────────────────────────────────────────────────── */
  function tornadoChart(rows, baseValue) {
    if (!rows || !rows.length) return '<div class="tbl-note">本次运行未产出敏感性数据。</div>';
    var W = 880, rowH = 32, padL = 132, padR = 26, padT = 24, padB = 30;
    var H = padT + rows.length * rowH + padB;
    var vals = [];
    rows.forEach(function (r) { vals.push(r.per_share_at_low, r.per_share_at_high); });
    vals.push(baseValue);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    var x = function (v) { return padL + (v - lo) / (hi - lo) * (W - padL - padR); };

    var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="单因素敏感性：各假设上下浮动对每股价值的影响">'];
    ticks(lo, hi, 6).forEach(function (t) {
      s.push('<line x1="' + n2(x(t)) + '" y1="' + (padT - 8) + '" x2="' + n2(x(t)) + '" y2="' + (H - padB + 2) +
        '" stroke="' + C.grid + '"/>');
      s.push('<text x="' + n2(x(t)) + '" y="' + (H - padB + 17) + '" fill="' + C.axis +
        '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">' + n2(t) + '</text>');
    });
    var xb = x(baseValue);
    s.push('<line x1="' + n2(xb) + '" y1="' + (padT - 12) + '" x2="' + n2(xb) + '" y2="' + (H - padB + 2) +
      '" stroke="' + C.axis + '" stroke-width="1.2" stroke-dasharray="4 4"/>');
    s.push('<text x="' + n2(xb) + '" y="' + (padT - 15) + '" fill="' + C.axis +
      '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">基准 ' + n2(baseValue) + '</text>');

    rows.forEach(function (r, i) {
      var y = padT + i * rowH + rowH / 2;
      var a = x(Math.min(r.per_share_at_low, r.per_share_at_high));
      var b = x(Math.max(r.per_share_at_low, r.per_share_at_high));
      var lowIsLeft = r.per_share_at_low <= r.per_share_at_high;
      s.push('<text x="' + (padL - 12) + '" y="' + (y + 3.5) + '" fill="' + C.text +
        '" font-size="11.5" text-anchor="end">' + esc(r.name) + '</text>');
      s.push('<rect x="' + n2(a) + '" y="' + (y - 7) + '" width="' + n2(Math.max(2, b - a)) +
        '" height="14" rx="7" fill="' + (i === 0 ? C.mint : C.indigo) + '" opacity=".28"/>');
      s.push('<line x1="' + n2(a) + '" y1="' + y + '" x2="' + n2(b) + '" y2="' + y +
        '" stroke="' + (i === 0 ? C.mint : C.indigo) + '" stroke-width="1.6"/>');
      s.push('<circle cx="' + n2(a) + '" cy="' + y + '" r="3" fill="' + C.strong + '"/>');
      s.push('<circle cx="' + n2(b) + '" cy="' + y + '" r="3" fill="' + C.strong + '"/>');
      s.push('<text x="' + (a - 8) + '" y="' + (y + 3.5) + '" fill="' + C.text +
        '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace" text-anchor="end">' +
        n2(lowIsLeft ? r.per_share_at_low : r.per_share_at_high) + '</text>');
      s.push('<text x="' + (b + 8) + '" y="' + (y + 3.5) + '" fill="' + C.text +
        '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace">' +
        n2(lowIsLeft ? r.per_share_at_high : r.per_share_at_low) + '</text>');
    });
    s.push('</svg>');
    return s.join('');
  }

  /* ── 3. 双因素热力网格（HTML 表格，便于读数与无障碍） ─────────────── */
  function heatmapTable(grid, price, fmt) {
    if (!grid || !grid.cells || !grid.cells.length) {
      return '<div class="tbl-note">本次运行未产出双因素网格。</div>';
    }
    var vals = [];
    grid.cells.forEach(function (row) { row.forEach(function (v) { if (v != null) vals.push(v); }); });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pctFmt = function (v) { return (v * 100).toFixed(2) + '%'; };
    var valFmt = fmt || n2;

    var s = ['<div class="tbl-wrap"><table class="tbl heat" data-price="' + price + '"><thead><tr>'];
    s.push('<th class="num" style="min-width:74px">' + esc(grid.row_name) + ' ↓ / ' + esc(grid.col_name) + ' →</th>');
    grid.col_values.forEach(function (c) { s.push('<th class="num">' + pctFmt(c) + '</th>'); });
    s.push('</tr></thead><tbody>');
    grid.row_values.forEach(function (rv, i) {
      s.push('<tr><td class="num dim">' + pctFmt(rv) + '</td>');
      (grid.cells[i] || []).forEach(function (v, j) {
        if (v == null) { s.push('<td class="num dim">—</td>'); return; }
        var t = (v - lo) / (hi - lo || 1);
        var below = v < price;
        var bg = below
          ? 'rgba(239,68,68,' + (0.10 + (1 - t) * 0.24).toFixed(3) + ')'
          : 'rgba(0,229,153,' + (0.06 + t * 0.26).toFixed(3) + ')';
        var col = below ? '#FCA5A5' : (t > 0.55 ? '#A7F3D0' : C.strong);
        /* 用 aria-label + 自绘浮层读数，而不是原生 title：
           原生提示有几秒延迟、不可样式化，触屏上根本不出现（PRD FE-C07）。 */
        s.push('<td class="num heat-cell" tabindex="0" data-rv="' + rv +
          '" data-cv="' + grid.col_values[j] + '" data-v="' + v + '" style="background:' + bg +
          ';color:' + col + '" aria-label="' + esc(grid.row_name) + ' ' + pctFmt(rv) + '，' +
          esc(grid.col_name) + ' ' + pctFmt(grid.col_values[j]) + '，每股价值 ' + valFmt(v) +
          (below ? '，低于现价' : '') + '">' + valFmt(v) + '</td>');
      });
      s.push('</tr>');
    });
    s.push('</tbody></table></div>');
    s.push('<div class="chart-legend"><span>色阶：每股价值越低越偏红，越高越偏绿</span>' +
      '<span>红框数字表示该组合下估值低于现价 ' + n2(price) + '</span></div>');
    return s.join('');
  }

  /* ── 4. 折线图（多序列） ───────────────────────────────────────────── */
  function lineChart(series, xLabels, opts) {
    opts = opts || {};
    if (!series || !series.length) return '';
    var W = 880, H = opts.height || 260, padL = 62, padR = 22, padT = 18, padB = 34;
    var all = [];
    series.forEach(function (s) { s.points.forEach(function (p) { if (p != null) all.push(p); }); });
    if (!all.length) return '';
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (opts.zeroBase) lo = Math.min(0, lo);
    var pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
    var n = series[0].points.length;
    var x = function (i) { return padL + (n <= 1 ? 0 : i / (n - 1)) * (W - padL - padR); };
    var y = function (v) { return padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB); };

    var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' +
      esc(opts.title || '趋势图') + '">'];
    ticks(lo, hi, 4).forEach(function (t) {
      s.push('<line x1="' + padL + '" y1="' + n2(y(t)) + '" x2="' + (W - padR) + '" y2="' + n2(y(t)) +
        '" stroke="' + C.grid + '"/>');
      s.push('<text x="' + (padL - 8) + '" y="' + (y(t) + 3.5) + '" fill="' + C.axis +
        '" font-size="10" font-family="JetBrains Mono, Consolas, monospace" text-anchor="end">' + (opts.fmt ? opts.fmt(t) : n2(t)) + '</text>');
    });
    (xLabels || []).forEach(function (lb, i) {
      s.push('<text x="' + n2(x(i)) + '" y="' + (H - padB + 17) + '" fill="' + C.axis +
        '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">' + esc(lb) + '</text>');
    });
    series.forEach(function (se) {
      var d = '', started = false;
      se.points.forEach(function (p, i) {
        if (p == null) { started = false; return; }
        d += (started ? 'L' : 'M') + n2(x(i)) + ' ' + n2(y(p)) + ' ';
        started = true;
      });
      if (se.area) {
        var d2 = '';
        se.points.forEach(function (p, i) {
          if (p == null) return;
          d2 += (d2 ? 'L' : 'M') + n2(x(i)) + ' ' + n2(y(p)) + ' ';
        });
        d2 += 'L' + n2(x(se.points.length - 1)) + ' ' + n2(y(lo)) + ' L' + padL + ' ' + n2(y(lo)) + ' Z';
        s.push('<path d="' + d2 + '" fill="' + se.color + '" opacity=".10"/>');
      }
      s.push('<path d="' + d + '" fill="none" stroke="' + se.color + '" stroke-width="' +
        (se.width || 2) + '"' + (se.dash ? ' stroke-dasharray="5 4"' : '') + '/>');
      se.points.forEach(function (p, i) {
        if (p == null) return;
        s.push('<circle cx="' + n2(x(i)) + '" cy="' + n2(y(p)) + '" r="' + (se.dot === false ? 0 : 3) +
          '" fill="' + C.strong + '" stroke="' + se.color + '" stroke-width="1.6"/>');
      });
      var last = null;
      for (var i = se.points.length - 1; i >= 0; i--) { if (se.points[i] != null) { last = i; break; } }
      if (last != null) {
        s.push('<text x="' + n2(x(last) + 8) + '" y="' + n2(y(se.points[last]) + 3.5) + '" fill="' +
          se.color + '" font-size="10.5" font-family="JetBrains Mono, Consolas, monospace">' +
          (opts.fmt ? opts.fmt(se.points[last]) : n2(se.points[last])) + '</text>');
      }
    });
    s.push('</svg>');
    return s.join('');
  }

  /* ── 5. 横向条形图（可比公司倍数、区间归因） ───────────────────────── */
  function barChart(items, opts) {
    opts = opts || {};
    if (!items || !items.length) return '<div class="tbl-note">无数据。</div>';
    var rowH = 30, padL = opts.padL || 116, padR = 66, padT = 12, padB = 14;
    var W = 880, H = padT + items.length * rowH + padB;
    var hi = Math.max.apply(null, items.map(function (i) { return Math.abs(i.value) || 0; })) || 1;
    var bw = W - padL - padR;
    var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.title || '条形图') + '">'];
    items.forEach(function (it, i) {
      var y = padT + i * rowH;
      var w = Math.max(2, Math.abs(it.value) / hi * bw);
      s.push('<text x="' + (padL - 10) + '" y="' + (y + 17) + '" fill="' + C.text +
        '" font-size="11.5" text-anchor="end">' + esc(it.label) + '</text>');
      s.push('<rect x="' + padL + '" y="' + (y + 5) + '" width="' + n2(bw) + '" height="14" rx="7" fill="rgba(255,255,255,.04)"/>');
      s.push('<rect x="' + padL + '" y="' + (y + 5) + '" width="' + n2(w) + '" height="14" rx="7" fill="' +
        (it.color || (i === 0 ? C.mint : C.indigo)) + '" opacity=".55" stroke="' +
        (it.color || (i === 0 ? C.mint : C.indigo)) + '" stroke-opacity=".6"/>');
      s.push('<text x="' + (padL + bw + 10) + '" y="' + (y + 17) + '" fill="' + C.strong +
        '" font-size="11.5" font-family="JetBrains Mono, Consolas, monospace">' + (it.text != null ? esc(it.text) : n2(it.value)) + '</text>');
    });
    s.push('</svg>');
    return s.join('');
  }

  /* ── 6. 迷你走势（KPI 卡内） ───────────────────────────────────────── */
  function sparkline(values, color) {
    if (!values || values.length < 2) return '';
    var W = 92, H = 26;
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    var x = function (i) { return i / (values.length - 1) * W; };
    var y = function (v) { return H - 3 - (v - lo) / (hi - lo || 1) * (H - 6); };
    var d = values.map(function (v, i) { return (i ? 'L' : 'M') + n2(x(i)) + ' ' + n2(y(v)); }).join(' ');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="92" height="26" aria-hidden="true">' +
      '<path d="' + d + '" fill="none" stroke="' + (color || C.mint) + '" stroke-width="1.8" stroke-linecap="round"/></svg>';
  }

  /* ── 7. 估值桥（横向阶梯） ─────────────────────────────────────────── */
  function bridgeChart(steps) {
    if (!steps || !steps.length) return '';
    var W = 880, H = 132, padL = 12, padR = 12, padT = 28;
    var bw = (W - padL - padR) / steps.length - 10;
    var hi = Math.max.apply(null, steps.map(function (s) { return Math.abs(s.value); })) || 1;
    var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="估值桥">'];
    steps.forEach(function (st, i) {
      var h = Math.max(6, Math.abs(st.value) / hi * 66);
      var x = padL + i * ((W - padL - padR) / steps.length) + 5;
      var y = padT + (66 - h);
      var col = st.value < 0 ? C.red : (i === steps.length - 1 ? C.mint : C.indigo);
      s.push('<rect x="' + n2(x) + '" y="' + n2(y) + '" width="' + n2(bw) + '" height="' + n2(h) +
        '" rx="5" fill="' + col + '" opacity=".28" stroke="' + col + '" stroke-opacity=".55"/>');
      s.push('<text x="' + n2(x + bw / 2) + '" y="' + n2(y - 7) + '" fill="' + C.strong +
        '" font-size="11" font-family="JetBrains Mono, Consolas, monospace" text-anchor="middle">' + n2(st.value) + '</text>');
      s.push('<text x="' + n2(x + bw / 2) + '" y="' + (padT + 84) + '" fill="' + C.text +
        '" font-size="10.5" text-anchor="middle">' + esc(st.label) + '</text>');
      if (i < steps.length - 1) {
        s.push('<text x="' + n2(x + bw + 5) + '" y="' + (padT + 40) + '" fill="' + C.axis +
          '" font-size="11" text-anchor="middle">→</text>');
      }
    });
    s.push('</svg>');
    return s.join('');
  }

  global.Charts = {
    colors: C,
    bandChart: bandChart,
    tornadoChart: tornadoChart,
    heatmapTable: heatmapTable,
    lineChart: lineChart,
    barChart: barChart,
    sparkline: sparkline,
    bridgeChart: bridgeChart,
    esc: esc,
    n2: n2,
    ticks: ticks
  };
})(window);
