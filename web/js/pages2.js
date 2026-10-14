/* 页面渲染层（续）：相对估值、勾稽校验、报告中心、复现凭证、历史运行、设置 */
(function (global) {
  'use strict';

  var P = global.Pages, F = P.F, card = P.card, kpi = P.kpi, kvRows = P.kvRows,
      hashChip = P.hashChip, demoBanner = P.demoBanner, esc = Charts.esc;
  var MC = Charts.colors;

  /* 简版 Markdown 渲染：标题、表格、列表、引用、粗体、行内代码、分隔线 */
  function md2html(md, opts) {
    if (!md) return '';
    opts = opts || {};
    var lines = String(md).replace(/\r/g, '').split('\n');
    var out = [], i = 0, inList = false, pendCap = null;
    function inline(s) {
      return esc(s)
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        /* 整行斜体：报告里的「代码判断（未使用大模型）」这类徽章行。
           用首尾锚定，避免把文件名、哈希里的下划线误当成斜体标记。 */
        .replace(/^_([^_]+)_$/, '<em>$1</em>')
        .replace(/【(事实|推论|观点)】/g, '<span class="badge $1">【$1】</span>')
        .replace(/【事实】/g, '<span class="badge fact">【事实】</span>')
        .replace(/【推论】/g, '<span class="badge infer">【推论】</span>')
        .replace(/【观点】/g, '<span class="badge opinion">【观点】</span>');
    }
    var CAP_RE = /^\*([^*]+\.(?:png|jpe?g|svg))\*$/;
    var IMG_RE = /^!\[([^\]]*)\]\(([^)]+)\)$/;
    function closeList() { if (inList) { out.push('</ul>'); inList = false; } }
    while (i < lines.length) {
      var ln = lines[i];
      if (/^\s*$/.test(ln)) { closeList(); i++; continue; }

      /* 图表文件名单独一行：暂存成图注，跟在图片下面 */
      var cap = ln.trim().match(CAP_RE);
      if (cap) { pendCap = cap[1]; i++; continue; }

      /* 图片：![说明](charts/15_forecast_trend.png) —— 报告里的六张图 */
      var img = ln.trim().match(IMG_RE);
      if (img) {
        closeList();
        var src = img[2];
        var url = (/^(https?:)?\/\//.test(src) || src.charAt(0) === '/')
          ? src : (opts.imageBase || '') + src;
        out.push('<figure class="fig"><img src="' + esc(url) + '" alt="' + esc(img[1] || '图表') +
          '" loading="lazy">' + (pendCap ? '<figcaption>' + esc(pendCap) + '</figcaption>' : '') +
          '</figure>');
        pendCap = null;
        i++; continue;
      }

      var h = ln.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        closeList();
        var lv = Math.min(3, Math.max(2, h[1].length));
        out.push('<h' + lv + '>' + inline(h[2] || '') + '</h' + lv + '>');
        i++; continue;
      }
      if (/^---+$/.test(ln.trim())) { closeList(); out.push('<hr>'); i++; continue; }
      if (/^\|/.test(ln) && i + 1 < lines.length && /^\|[\s:\-|]+\|$/.test(lines[i + 1].trim())) {
        closeList();
        var head = ln.split('|').slice(1, -1).map(function (c) { return '<th>' + inline(c.trim()) + '</th>'; }).join('');
        i += 2;
        var body = '';
        while (i < lines.length && /^\|/.test(lines[i])) {
          body += '<tr>' + lines[i].split('|').slice(1, -1).map(function (c, k) {
            return '<td' + (k ? ' class="num"' : '') + '>' + inline(c.trim()) + '</td>';
          }).join('') + '</tr>';
          i++;
        }
        out.push('<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>');
        continue;
      }
      if (/^>\s?/.test(ln)) {
        closeList();
        var q = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, '')); i++; }
        out.push('<blockquote>' + q.map(inline).join('<br>') + '</blockquote>');
        continue;
      }
      if (/^[-*]\s+/.test(ln)) {
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + inline(ln.replace(/^[-*]\s+/, '')) + '</li>');
        i++; continue;
      }
      closeList();
      var para = [ln];
      i++;
      while (i < lines.length && !/^\s*$/.test(lines[i]) &&
             !/^(#{1,4})\s|^\||^>\s?|^[-*]\s|^---+$/.test(lines[i]) &&
             !IMG_RE.test(lines[i].trim()) && !CAP_RE.test(lines[i].trim())) {
        para.push(lines[i]); i++;
      }
      out.push('<p>' + para.map(inline).join('<br>') + '</p>');
    }
    closeList();
    if (pendCap) out.push('<p class="figcap">' + esc(pendCap) + '</p>');
    return out.join('\n');
  }

  /* 可比公司：兼容两种数据形状（演示数据 / 真实 07 号 CSV） */
  function multsOf(c) {
    if (c.pe != null || c.pb != null) return c;
    var mc = c.market_cap, ev = c.enterprise_value;
    var pos = function (x) { return (x != null && isFinite(x) && x > 0) ? x : null; };
    return {
      pe: c.net_profit_attributable ? pos(mc / c.net_profit_attributable) : null,
      pb: c.book_equity_attributable ? pos(mc / c.book_equity_attributable) : null,
      ps: c.revenue ? pos(mc / c.revenue) : null,
      ev_ebitda: c.ebitda ? pos(ev / c.ebitda) : null,
      ev_ebit: c.ebit ? pos(ev / c.ebit) : null
    };
  }
  var MKEYS = ['pe', 'pb', 'ps', 'ev_ebitda', 'ev_ebit'];
  var MLABEL = { pe: 'PE', pb: 'PB', ps: 'PS', ev_ebitda: 'EV/EBITDA', ev_ebit: 'EV/EBIT' };

  /* ══════════════════════════════════════════════════════════════════
     页面 7：相对估值
     ══════════════════════════════════════════════════════════════════ */
  var comps = {
    id: 'comps', nav: '相对估值', icon: 'scale', group: '分析工作台',
    title: '相对估值', sub: function () { return '可比公司倍数、隐含价值与估值区间'; },
    render: function (c) {
      var d = c.data, s = P.valSummary(d);
      var cp = d.comps, ff = d.football_field || {};
      var out = [];
      out.push(demoBanner(d));
      if (!cp || !cp.target) {
        out.push(P.emptyBlock('scale', '本次未产出相对估值', '缺少 07_comps.csv，通常是因为可比公司数据缺失。'));
        return out.join('');
      }
      var t = cp.target, peers = cp.peers || [], stats = cp.stats || {}, implied = cp.implied || [];
      var tm = multsOf(t);
      var meds = implied.map(function (x) { return x.implied_per_share; }).filter(function (x) { return x != null; });
      var lo = Math.min.apply(null, meds.concat([s.per])), hi = Math.max.apply(null, meds.concat([s.per]));
      var labels = implied.map(function (x) { return MLABEL[x.multiple] || x.label || x.multiple; });
      var inRangeVals = meds.filter(function (x) { return x <= s.per; });

      out.push('<div class="grid g-4">' +
        kpi('DCF 每股价值', F.price(s.per), '元', '<span class="muted">现金流折现</span>', { icon: 'target' }) +
        kpi('相对估值区间', F.price(Math.min.apply(null, meds || [s.per])) + ' – ' + F.price(Math.max.apply(null, meds || [s.per])), '元',
          '<span class="muted">' + meds.length + ' 种倍数口径</span>', { hero: true }) +
        kpi('整体估值区间', F.price(ff.overall_low) + ' – ' + F.price(ff.overall_high), '元',
          '<span class="muted">含 DCF 与倍数区间</span>') +
        kpi('低于现价的方法', String(inRangeVals.length) + ' / ' + meds.length, '',
          '<span class="muted">现价 ' + F.price(s.price) + ' 元</span>') +
        '</div>');

      var left = [];
      left.push(card('估值区间：各方法并轨（Football Field）', 'scale',
        '<div class="chart">' + Charts.bandChart(ff.bands || [], ff.price || s.price) + '</div>' +
        (ff.warnings && ff.warnings.length ? '<div class="tbl-note">' +
          ff.warnings.map(function (w) { return '· ' + esc(w); }).join('<br>') + '</div>' : ''), {}));

      function rowOf(kind, x) {
        var m = multsOf(x);
        return '<tr' + (kind === '目标' ? ' style="background:rgba(0,229,153,.05)"' : '') + '>' +
          '<td data-label="类型" data-sort="' + esc(kind) + '"><span class="badge ' + (kind === '目标' ? 'pass' : 'neutral') + '">' + kind + '</span></td>' +
          '<td class="mono" data-label="代码" data-sort="' + esc(x.code) + '">' + esc(x.code) + '</td>' +
          '<td data-label="名称" data-sort="' + esc(x.name) + '">' + esc(x.name) + '</td>' +
          '<td class="num" data-label="股价" data-sort="' + (x.price == null ? '' : x.price) + '">' + F.price(x.price) + '</td>' +
          '<td class="num" data-label="市值(亿元)" data-sort="' + (x.market_cap == null ? '' : x.market_cap) + '">' + F.moneyShort(x.market_cap) + '</td>' +
          '<td class="num" data-label="企业价值(亿元)" data-sort="' + (x.enterprise_value == null ? '' : x.enterprise_value) + '">' + F.moneyShort(x.enterprise_value) + '</td>' +
          MKEYS.map(function (k) {
            return '<td class="num" data-label="' + MLABEL[k] + '" data-sort="' +
              (m[k] == null ? '' : m[k]) + '">' + F.x(m[k]) + '</td>';
          }).join('') + '</tr>';
      }
      left.push(card('可比公司倍数表', 'table',
        '<div class="tbl-wrap"><table class="tbl responsive pairs"><thead><tr>' +
        '<th data-col="0">类型</th><th data-col="1">代码</th><th data-col="2">名称</th>' +
        '<th class="num" data-col="3">股价</th><th class="num" data-col="4">市值</th>' +
        '<th class="num" data-col="5">企业价值</th>' +
        MKEYS.map(function (k, i) {
          return '<th class="num" data-col="' + (6 + i) + '">' + MLABEL[k] + '</th>';
        }).join('') +
        '</tr></thead><tbody>' + rowOf('目标', t) + peers.map(function (p) { return rowOf('可比', p); }).join('') +
        '</tbody></table></div><div class="tbl-note">市值与企业价值单位为亿元；倍数由程序计算，分母非正的样本不参与统计。' +
        '点击表头可排序，缺失值始终排在最后。</div>', {}));

      if (Object.keys(stats).length) {
        left.push(card('倍数分布统计', 'filter',
          '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
          '<th>统计口径</th><th class="num">样本数</th><th class="num">下四分位</th><th class="num">中位数</th><th class="num">上四分位</th><th class="num">均值</th></tr></thead><tbody>' +
          MKEYS.filter(function (k) { return stats[k]; }).map(function (k) {
            var st = stats[k];
            return '<tr><td data-label="口径">' + (st.label || MLABEL[k]) + '</td>' +
              '<td class="num" data-label="样本数">' + F.int(st.count) + '</td>' +
              '<td class="num" data-label="下四分位">' + F.x(st.q1) + '</td>' +
              '<td class="num" data-label="中位数">' + F.x(st.median) + '</td>' +
              '<td class="num" data-label="上四分位">' + F.x(st.q3) + '</td>' +
              '<td class="num" data-label="均值">' + F.x(st.mean) + '</td></tr>';
          }).join('') + '</tbody></table></div>' +
          '<div class="tbl-note">异常值剔除默认关闭 —— 可比公司通常只有三到五家，自动剔除会显著改变结论。</div>', {}));
      }

      var right = [];
      right.push(card('隐含每股价值', 'target',
        (implied.length ? '<div class="list">' + implied.map(function (x) {
          var rel = x.implied_per_share != null ? (x.implied_per_share / s.price - 1) : null;
          return '<div class="list-item"><span class="badge neutral">' + esc(MLABEL[x.multiple] || x.label) + '</span>' +
            '<div class="li-main"><div class="mono">' + F.price(x.implied_per_share) + ' 元</div>' +
            '<div class="li-sub">可比公司倍数 ' + F.x(x.peer_value) + '</div></div>' +
            '<div class="li-right">' + (rel == null ? '' : '<span class="delta ' + (rel >= 0 ? 'up' : 'down') + '">' +
              F.pctSigned(rel) + '</span>') + '</div></div>';
        }).join('') + '</div>' : '<div class="muted small">没有可用的隐含估值。</div>'),
        { right: P.evCompsImplied(d, implied[0], t.net_debt, t.diluted_shares) }));

      var excluded = [];
      Object.keys(stats).forEach(function (k) {
        (stats[k].excluded || []).forEach(function (x) { excluded.push([MLABEL[k], x[0], x[1]]); });
      });
      right.push(card('被排除的样本', 'alert',
        excluded.length ? '<div class="list">' + excluded.map(function (x) {
          return '<div class="list-item"><span class="badge skip">' + esc(x[0]) + '</span>' +
            '<div class="li-main"><span class="mono">' + esc(x[1]) + '</span>' +
            '<div class="li-sub">' + esc(x[2]) + '</div></div></div>';
        }).join('') + '</div><div class="tbl-note">分母为负时倍数没有意义，这些公司不参与统计，也不显示为 0。</div>'
          : '<div class="badge pass">' + Icons.svg('check') + '没有样本被排除</div>', {}));

      var overlap = !(ff.bands && ff.bands.length > 1) ? '—'
        : (ff.overall_low <= s.per && s.per <= ff.overall_high ? '现价落在整体区间内' : '现价在整体区间之外');
      right.push(card('一致性结论', 'info',
        '<div class="prose" style="font-size:13px">' +
        '<p>DCF 每股价值 <b>' + F.price(s.per) + ' 元</b>，相对估值区间 <b>' +
        F.price(Math.min.apply(null, meds || [0])) + ' – ' + F.price(Math.max.apply(null, meds || [0])) + ' 元</b>。</p>' +
        '<p>' + (ff.warnings && ff.warnings.length ? esc(ff.warnings[0]) :
          '两条估值线是否互相支持，取决于区间是否重叠；不重叠说明至少有一边的假设需要复核。') + '</p>' +
        '<p class="muted small">' + esc(overlap) + '</p></div>', {}));

      out.push('<div class="grid g-7-5 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    },
    /* 表头排序（PRD FE-C11）：纯前端重排，不改变任何数值口径 */
    mount: function (root) {
      var table = root.querySelector('table.pairs');
      if (!table) return;
      var dir = {};
      table.querySelectorAll('th[data-col]').forEach(function (th) {
        th.style.cursor = 'pointer';
        th.title = '点击排序';
        th.addEventListener('click', function () {
          var col = Number(th.getAttribute('data-col'));
          var asc = !dir[col];
          dir[col] = asc;
          var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr'));
          var val = function (tr) {
            var td = tr.children[col];
            var raw = td ? td.getAttribute('data-sort') : '';
            if (raw === '' || raw == null) return null;
            var n = Number(raw);
            return isFinite(n) ? n : String(raw);
          };
          rows.sort(function (a, b) {
            var av = val(a), bv = val(b);
            if (av == null && bv == null) return 0;
            if (av == null) return 1;      // 缺失值永远排最后
            if (bv == null) return -1;
            var cmp = (typeof av === 'string' || typeof bv === 'string')
              ? String(av).localeCompare(String(bv), 'zh-CN')
              : av - bv;
            return asc ? cmp : -cmp;
          });
          rows.forEach(function (r) { table.tBodies[0].appendChild(r); });
          table.querySelectorAll('th[data-col]').forEach(function (h) { h.removeAttribute('aria-sort'); });
          th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        });
      });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 8：勾稽校验
     ══════════════════════════════════════════════════════════════════ */
  var validation = {
    id: 'validation', nav: '勾稽校验', icon: 'shield', group: '交付与凭证',
    title: '财务勾稽校验', sub: function () { return '逐年逐条的恒等式与信号类检查结论'; },
    render: function (c) {
      var d = c.data, vr = d.validation;
      var out = [];
      out.push(demoBanner(d));
      if (!vr || !vr.reports || !vr.reports.length) {
        out.push(P.emptyBlock('shield', '本次未产出校验报告', '缺少 02_validation_report.json，无法判断数据是否可信。'));
        return out.join('');
      }
      var reports = vr.reports;
      var tot = { pass: 0, warning: 0, fail: 0, skipped: 0, total: 0 };
      reports.forEach(function (r) {
        ['pass', 'warning', 'fail', 'skipped', 'total'].forEach(function (k) { tot[k] += r.summary[k] || 0; });
      });
      var rate = tot.total ? (tot.pass + tot.warning) / tot.total : 0;

      out.push('<div class="grid g-4">' +
        kpi('通过率', F.pct(rate, 1), '', '<span class="muted">通过 + 告警 / 全部检查</span>', { hero: true, icon: 'shield' }) +
        kpi('检查项', F.int(tot.total), '项', '<span class="muted">' + reports.length + ' 个年度 × ' + (reports[0].checks.length) + ' 项</span>') +
        kpi('告警', F.int(tot.warning), '项', '<span class="muted">有差异但存在解释空间</span>') +
        kpi('失败 / 跳过', F.int(tot.fail) + ' / ' + F.int(tot.skipped), '项', '<span class="muted">跳过不等于通过</span>') +
        '</div>');

      /* 年度分布图 */
      var vItems = reports.map(function (r) {
        return { label: String(r.fiscal_year), value: r.summary.total || 1,
          text: '通过 ' + r.summary.pass + ' · 告警 ' + r.summary.warning + ' · 失败 ' + r.summary.fail,
          color: r.summary.fail ? MC.red : (r.summary.warning ? MC.amber : MC.mint) };
      });
      out.push('<div class="grid g-8-4 mt14">' +
        card('各年度校验分布', 'layers',
          '<div class="chart">' + Charts.barChart(vItems, { padL: 64, title: '各年度检查结果' }) + '</div>', {}) +
        card('检查项清单（不硬编码，取自产物）', 'listChecks',
          '<div class="list">' + reports[0].checks.map(function (ch) {
            return '<div class="list-item"><span class="badge neutral">' + esc(ch.check_id) + '</span>' +
              '<div class="li-main"><div class="small">' + esc(ch.category) + '</div>' +
              '<div class="li-sub">' + esc(ch.description) + '</div></div></div>';
          }).join('') + '</div>', {}) + '</div>');

      /* 明细：按年度展开 */
      var detail = reports.map(function (r, ri) {
        var rows = r.checks.map(function (ch) {
          var extra = ch.note ? '<div class="li-sub">' + esc(ch.note) + '</div>' : '';
          return '<tr data-status="' + esc(ch.status) + '">' +
            '<td class="mono" data-label="编号">' + esc(ch.check_id) + '</td>' +
            '<td data-label="类别">' + esc(ch.category) + '</td>' +
            '<td data-label="检查内容">' + esc(ch.description) + extra + '</td>' +
            '<td data-label="结论">' + P.statusBadge(ch.status) + '</td>' +
            '<td class="num" data-label="差异(元)">' + (ch.difference != null ? F.num(ch.difference, 2) : '—') + '</td>' +
            '<td class="num" data-label="指标值">' + (ch.metric != null ? F.num(ch.metric, 4) : '—') + '</td>' +
            '<td class="num" data-label="阈值/容差">' + (ch.tolerance != null ? F.num(ch.tolerance, 4) : '—') + '</td></tr>';
        }).join('');
        return '<div class="v-year" data-year="' + r.fiscal_year + '"' + (ri ? ' hidden' : '') + '>' +
          '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
          '<th>编号</th><th>类别</th><th>检查内容</th><th>结论</th>' +
          '<th class="num">差异（元）</th><th class="num">指标值</th><th class="num">阈值</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table></div></div>';
      }).join('');

      out.push(card('逐年逐条结论', 'table',
        '<div class="row wrap mb12"><div class="row" style="gap:6px" id="yrTabs">' +
        reports.map(function (r, i) {
          return '<button class="btn sm ' + (i ? 'ghost' : 'on') + '" data-year="' + r.fiscal_year + '" type="button">' +
            r.fiscal_year + ' 年</button>';
        }).join('') + '</div><div class="spacer"></div>' +
        '<div class="row" style="gap:6px" id="stFilter">' +
        '<button class="btn sm on" data-st="all" type="button">全部</button>' +
        '<button class="btn sm ghost" data-st="warning" type="button">仅告警</button>' +
        '<button class="btn sm ghost" data-st="fail" type="button">仅失败</button>' +
        '<button class="btn sm ghost" data-st="skipped" type="button">仅跳过</button>' +
        '</div></div>' + detail +
        '<div class="tbl-note">差异与指标值的口径不同：恒等式类检查填差异（元），信号类检查填指标值，其阈值为触发条件而非容差。</div>', {}));

      return out.join('');
    },
    mount: function (root) {
      var state = { year: null, st: 'all' };
      function apply() {
        root.querySelectorAll('.v-year').forEach(function (el) {
          var show = state.year == null || String(el.getAttribute('data-year')) === String(state.year);
          el.hidden = !show;
        });
        root.querySelectorAll('.v-year tr[data-status]').forEach(function (tr) {
          tr.hidden = state.st !== 'all' && tr.getAttribute('data-status') !== state.st;
        });
      }
      root.querySelectorAll('#yrTabs button').forEach(function (b) {
        b.addEventListener('click', function () {
          state.year = b.getAttribute('data-year');
          root.querySelectorAll('#yrTabs button').forEach(function (x) {
            x.className = 'btn sm ' + (x === b ? 'on' : 'ghost');
          });
          apply();
        });
      });
      root.querySelectorAll('#stFilter button').forEach(function (b) {
        b.addEventListener('click', function () {
          state.st = b.getAttribute('data-st');
          root.querySelectorAll('#stFilter button').forEach(function (x) {
            x.className = 'btn sm ' + (x === b ? 'on' : 'ghost');
          });
          apply();
        });
      });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 9：报告中心
     ══════════════════════════════════════════════════════════════════ */
  var report = {
    id: 'report', nav: '报告中心', icon: 'book', group: '交付与凭证',
    title: '报告中心', sub: function (c) { return '十章结构 · 事实 / 推论 / 观点分级标注 · ' + c.data.run_id; },
    render: function (c) {
      var d = c.data, nv = (d.manifest && d.manifest.inputs && d.manifest.inputs.narrative) || null;
      var md = d.report_md;
      var out = [];
      out.push(demoBanner(d));
      if (!md && !d.demo) {
        out.push('<div class="banner warn">' + Icons.svg('alert') +
          '<div>本次运行没有产出 17_report.md；报告可能尚未生成，或该次运行只跑了部分阶段。</div></div>');
      }
      if (nv && !nv.ok) {
        out.push('<div class="banner warn">' + Icons.svg('alert') +
          '<div><b>数据版本报告</b> — 本次没有调用大模型，叙述段落被替换为说明文字，报告不满足提交要求。</div></div>');
      }

      var chapters = [];
      (String(md || '').match(/^##\s+.*$/gm) || []).forEach(function (h) {
        var t = h.replace(/^##\s+/, '');
        chapters.push(t);
      });

      var arts = d.artifacts || [];
      var has = function (name) { return arts.some(function (a) { return a.file === name; }); };
      /* 三种格式在页面内切换预览：MD 直接渲染、HTML / PDF 用同源 iframe 内嵌
         （PRD FE-E02）。点不开的格式说明本次没有产出该文件。 */
      var FMTS = [['md', 'MD', '17_report.md'], ['html', 'HTML', '18_report.html'],
                  ['pdf', 'PDF', '19_report.pdf']];
      var formats = FMTS.map(function (f) {
        var enabled = f[0] === 'md' || (has(f[2]) && !d.demo);
        return '<button class="btn sm ' + (f[0] === 'md' ? 'on' : 'ghost') + '" type="button" ' +
          'data-fmt="' + f[0] + '"' + (enabled ? '' : ' disabled title="本次未产出该格式"') + '>' +
          f[1] + '</button>';
      }).join('') +
        '<a class="btn sm ghost" id="fmtExternal" target="_blank" rel="noopener"' +
        (d.demo ? ' hidden' : '') + '>' + Icons.svg('external') + '新标签打开</a>';

      var nw = (nv && nv.number_warnings) || [];
      out.push('<div class="grid g-8-4">' +
        card('', null,
          '<div class="row wrap" style="gap:10px">' +
          '<span class="chip"><span class="k">章节</span><b>' + chapters.length + '</b></span>' +
          '<span class="chip"><span class="k">模型</span><b>' + esc(nv ? (nv.model || '未调用') : '未调用') + '</b></span>' +
          '<span class="chip"><span class="k">提示词</span><b>' + esc(nv ? (nv.prompt_version || '—') : '—') + '</b></span>' +
          '<span class="chip"><span class="k">数字校验</span><b>' + (nw.length === 0 ? '通过' : nw.length + ' 处待核') + '</b></span>' +
          '<span class="spacer"></span><div class="row" style="gap:6px">' + formats + '</div></div>', { cls: 'tight' }) +
        card('', null, '<div class="row wrap" style="gap:8px">' +
          '<span class="badge fact">【事实】年报或市场数据里直接读到的数</span>' +
          '<span class="badge infer">【推论】基于事实推导出来的判断</span>' +
          '<span class="badge opinion">【观点】我们的价值判断</span>' +
          '<span class="badge ai">' + Icons.svg('sparkles') + 'AI 生成</span>' +
          '<span class="badge code">代码判断</span></div>', { cls: 'tight' }) +
        '</div>');

      var nav = chapters.length
        ? '<div class="chapter-nav">' + chapters.map(function (t, i) {
            return '<button type="button" data-ch="ch-' + i + '"><span class="cn-idx">' +
              String(i + 1).padStart(2, '0') + '</span><span>' + esc(t.replace(/^\d+\.\s*/, '')) + '</span></button>';
          }).join('') + '</div>'
        : '<div class="muted small">没有可用的章节结构。</div>';

      var previewUrl = function (fmt) {
        if (d.demo) return '';
        return API.fileUrl(d.run_id, fmt === 'pdf' ? '19_report.pdf' : '18_report.html');
      };

      var right = [];
      right.push(card('数字校验', 'listChecks',
        nw.length === 0
          ? '<div class="badge pass">' + Icons.svg('check') + '未发现事实表之外的数字</div>' +
            '<div class="tbl-note">做法：把正文里出现的每一个数字与事实表逐一比对。</div>'
          : '<div class="list">' + nw.map(function (x) {
              return '<div class="list-item"><span class="badge warn">待核</span><div class="li-main">' + esc(x) + '</div></div>';
            }).join('') + '</div>', {}));

      right.push(card('大模型使用记录', 'sparkles',
        nv ? kvRows([
          ['模型', '<span class="mono">' + esc(nv.model || '—') + '</span>'],
          ['接口', '<span class="mono small">' + esc(nv.endpoint || '—') + '</span>'],
          ['提示词版本', '<span class="mono">' + esc(nv.prompt_version || '—') + '</span>'],
          ['温度', nv.temperature != null ? String(nv.temperature) : '—'],
          ['轮次', String(nv.rounds || '—')],
          ['耗时', F.dur(nv.elapsed_s)],
          ['token 用量', nv.usage ? F.int(nv.usage.total_tokens) : '—'],
          ['提示词哈希', hashChip(nv.prompt_sha256)],
          ['响应哈希', hashChip(nv.response_sha256)]
        ]) : '<div class="muted small">本次未调用大模型。</div>', { aiIcon: true }));

      right.push(card('图表索引', 'folder',
        '<div class="list">' + (arts.filter(function (a) { return /\.png$/i.test(a.file); }).length
          ? arts.filter(function (a) { return /\.png$/i.test(a.file); }).map(function (a) {
              return '<div class="list-item"><span class="badge neutral">图</span><div class="li-main">' +
                '<div class="mono small">' + esc(a.file.split('/').pop()) + '</div>' +
                '<div class="li-sub">' + (a.name || '') + '</div></div>' +
                (d.demo ? '' : '<div class="li-right"><button class="icon-btn" data-img="' + esc(a.file) +
                  '" type="button" aria-label="放大">' + Icons.svg('eye') + '</button></div>') + '</div>';
            }).join('')
          : '<div class="muted small">本次没有图表产物（可能未安装 matplotlib）。</div>') + '</div>', {}));

      out.push('<div class="report-grid" id="reportGrid">' +
        '<div id="reportNav">' + nav + '</div>' +
        card('', null,
          '<article class="prose" id="reportBody">' +
          (md ? md2html(md, {
            imageBase: d.demo ? '' : '/runs/' + encodeURIComponent(d.run_id) + '/'
          }) : '<p class="muted">没有报告正文。</p>') + '</article>' +
          '<div id="reportFrame" hidden data-html="' + esc(previewUrl('html')) +
          '" data-pdf="' + esc(previewUrl('pdf')) + '"></div>', { cls: 'tight' }) +
        '<div class="grid" style="gap:14px">' + right.join('') + '</div>' +
        '</div>');

      return out.join('');
    },
    mount: function (root, c) {
      /* 章节锚点 */
      var heads = root.querySelectorAll('#reportBody h2');
      heads.forEach(function (h, i) { h.id = 'ch-' + i; });
      root.querySelectorAll('.chapter-nav button').forEach(function (b) {
        b.addEventListener('click', function () {
          var t = root.querySelector('#' + b.getAttribute('data-ch'));
          if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
          root.querySelectorAll('.chapter-nav button').forEach(function (x) { x.classList.remove('on'); });
          b.classList.add('on');
        });
      });
      if (heads.length) root.querySelector('.chapter-nav button').classList.add('on');

      /* 格式切换：MD 用页面内渲染，HTML / PDF 用同源 iframe 内嵌预览 */
      var body = root.querySelector('#reportBody');
      var frame = root.querySelector('#reportFrame');
      var navCol = root.querySelector('#reportNav');
      var ext = root.querySelector('#fmtExternal');
      function urlFor(fmt) {
        if (!frame) return '';
        if (fmt === 'pdf') return frame.getAttribute('data-pdf') || '';
        if (fmt === 'html') return frame.getAttribute('data-html') || '';
        return '';
      }
      function setExternal(fmt) {
        if (!ext) return;
        var u = urlFor(fmt);
        if (u) { ext.setAttribute('href', u); ext.removeAttribute('hidden'); }
        else { ext.setAttribute('hidden', ''); }
      }
      setExternal('md');
      root.querySelectorAll('[data-fmt]').forEach(function (b) {
        b.addEventListener('click', function () {
          var fmt = b.getAttribute('data-fmt');
          root.querySelectorAll('[data-fmt]').forEach(function (x) {
            x.className = 'btn sm ' + (x === b ? 'on' : 'ghost');
          });
          setExternal(fmt);
          if (fmt === 'md') {
            if (frame) { frame.hidden = true; frame.innerHTML = ''; }
            if (body) body.hidden = false;
            if (navCol) navCol.hidden = false;
            return;
          }
          var u = urlFor(fmt);
          if (!u || !frame) return;
          if (body) body.hidden = true;
          if (navCol) navCol.hidden = true;    // 章节锚点只对 Markdown 预览有效
          frame.hidden = false;
          frame.innerHTML = '<iframe class="preview-frame" title="' +
            (fmt === 'pdf' ? 'PDF 报告预览' : 'HTML 报告预览') + '" src="' + u + '"></iframe>';
        });
      });

      /* 打开原始产物 */
      root.querySelectorAll('[data-open]').forEach(function (b) {
        b.addEventListener('click', function () {
          if (c.data.demo) return;
          window.open(API.fileUrl(c.data.run_id, b.getAttribute('data-open')), '_blank', 'noopener');
        });
      });
      root.querySelectorAll('[data-img]').forEach(function (b) {
        b.addEventListener('click', function () {
          c.lightbox(b.getAttribute('data-img'), API.fileUrl(c.data.run_id, b.getAttribute('data-img')));
        });
      });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 10：复现凭证
     ══════════════════════════════════════════════════════════════════ */
  var evidence = {
    id: 'evidence', nav: '复现凭证', icon: 'fingerprint', group: '交付与凭证',
    title: '可验证 · 可追溯 · 可复现', sub: function (c) { return '运行编号 ' + c.data.run_id; },
    render: function (c) {
      var d = c.data;
      var code = (d.manifest && d.manifest.inputs && d.manifest.inputs.code) || (d.logManifest && d.logManifest.code) || {};
      var arts = d.artifacts || [], inputs = d.inputs || [];
      var out = [];
      out.push(demoBanner(d));

      out.push('<div class="grid g-4">' +
        kpi('输入文件', F.int(inputs.length), '个', '<span class="muted">全部登记 sha256</span>', { icon: 'database' }) +
        kpi('产物文件', F.int(arts.length), '个', '<span class="muted">含图表与三份报告</span>', { hero: true, icon: 'folder' }) +
        kpi('执行阶段', F.int((d.steps || []).length), '步', '<span class="muted">逐步可追溯</span>') +
        kpi('代码版本', code.commit_short ? code.commit_short : '—', '',
          code.dirty ? '<span class="badge warn">工作区有未提交改动</span>' : '<span class="badge pass">与提交一致</span>') +
        '</div>');

      var left = [];
      left.push(card('数据来源（输入文件）', 'database',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr><th>文件</th><th>sha256</th><th class="num">大小</th></tr></thead><tbody>' +
        (inputs.length ? inputs.map(function (x) {
          return '<tr><td class="mono" data-label="文件">' + esc(x.path) + '</td>' +
            '<td data-label="sha256">' + hashChip(x.sha256) + '</td>' +
            '<td class="num" data-label="大小">' + (x.bytes ? F.bytes(x.bytes) : '—') + '</td></tr>';
        }).join('') : '<tr><td colspan="3" class="dim">本次未登记输入文件</td></tr>') +
        '</tbody></table></div><div class="tbl-note">哈希由流水线在运行时计算并写入清单，可用于核对输入是否被改动。</div>', {}));

      left.push(card('产物清单', 'folder',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr><th>文件</th><th>类型</th><th class="num">大小</th><th>sha256</th></tr></thead><tbody>' +
        (arts.length ? arts.map(function (a) {
          return '<tr><td class="mono" data-label="文件">' + esc(a.file) + '</td>' +
            '<td data-label="类型"><span class="badge neutral">' + esc(a.kind || (a.name || '')) + '</span></td>' +
            '<td class="num" data-label="大小">' + F.bytes(a.bytes) + '</td>' +
            '<td data-label="sha256">' + hashChip(a.sha256) + '</td></tr>';
        }).join('') : '<tr><td colspan="4" class="dim">本次未产出文件</td></tr>') +
        '</tbody></table></div>', { right: '<span class="badge neutral">' + arts.length + ' 个文件</span>' }));

      var right = [];
      right.push(card('代码与环境', 'gitBranch', kvRows([
        ['commit', code.commit ? '<span class="mono small">' + esc(F.short(code.commit, 12)) + '…</span>' : '—'],
        ['分支', '<span class="mono">' + esc(code.branch || '—') + '</span>'],
        ['工作区', code.dirty ? '<span class="badge warn">dirty</span>' : '<span class="badge pass">clean</span>'],
        ['Python', '<span class="mono">' + esc((d.manifest && d.manifest.python) || '—') + '</span>'],
        ['平台', '<span class="mono small">' + esc((d.manifest && d.manifest.platform) || '—') + '</span>'],
        ['轨迹哈希', d.logManifest && d.logManifest.trace_sha256 ? hashChip(d.logManifest.trace_sha256) : '—']
      ]), {}));

      var recCmd = 'python -m pipeline.cli check RUN=' + d.run_id;
      right.push(card('复现指令', 'terminal',
        '<pre class="code">' + esc('git checkout ' + (code.commit_short || '<commit>') + '\n' + recCmd) + '</pre>' +
        '<button class="btn sm ghost mt8" data-copy="' + esc(recCmd) + '" type="button">' + Icons.svg('copy') + '复制复现命令</button>', {}));

      var steps = (d.steps || []).slice(0, 8);
      right.push(card('执行轨迹（前 8 步）', 'activity',
        steps.length ? '<div class="timeline">' + steps.map(function (s) {
          return '<div class="tl-item ' + (s.status === 'ok' ? 'ok' : 'fail') + '">' +
            '<span class="tl-dot">' + (s.seq || '') + '</span>' +
            '<div><div class="tl-head"><span class="tl-name">' + esc(s.step) + '</span>' +
            P.statusBadge(s.status) + '<span class="badge neutral">' + F.dur(s.duration_s) + '</span></div>' +
            '<div class="tl-body">' + esc(s.tool || '') + (s.model ? ' · ' + esc(s.model) : '') + '</div></div></div>';
        }).join('') + '</div>' : '<div class="muted small">没有轨迹记录。</div>', {}));

      var boundary = [
        ['数据文件、图表', '逐字节相同', 'pass'],
        ['报告正文（Markdown）', '可复现（除运行编号与出具时间）', 'pass'],
        ['报告 HTML / PDF', '非逐字节相同（含生成时间戳）', 'warn'],
        ['大模型撰写的段落', '不可复现 —— 同样输入两次措辞不同', 'warn']
      ];
      out.push('<div class="grid g-7-5 mt14"><div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') +
        card('可复现性的边界', 'info',
          '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr><th>产物</th><th>可复现性</th></tr></thead><tbody>' +
          boundary.map(function (b) {
            return '<tr><td data-label="产物">' + esc(b[0]) + '</td>' +
              '<td data-label="可复现性"><span class="badge ' + b[2] + '">' + esc(b[1]) + '</span></td></tr>';
          }).join('') + '</tbody></table></div>' +
          '<div class="tbl-note">对于不可复现的部分，本项目把能固定的全部固定：模型名、接口、温度、推理强度、' +
          '提示词版本、提示词与响应的 sha256、token 用量，全部写进运行清单。</div>', {}) + '</div>');

      return out.join('');
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 11：历史运行
     ══════════════════════════════════════════════════════════════════ */
  var runs = {
    id: 'runs', nav: '历史运行', icon: 'history', group: '交付与凭证',
    title: '历史运行', sub: function () { return '两次运行的差异可以判断结果为什么变了'; },
    render: function (c) {
      var st = c.status || {};
      var recent = (st.recent || []).slice();
      var cur = c.data.run_id;
      if (!recent.some(function (r) { return r.run_id === cur; })) {
        recent.unshift({ run_id: cur, company: c.data.company.name, created_at: c.data.created_at, ok: true });
      }
      var out = [];
      out.push(demoBanner(c.data));

      if (!recent.length) {
        out.push(P.emptyBlock('history', '还没有历史运行', '跑一次估值之后，这里会列出每次运行的编号、标的与状态。',
          '<button class="btn primary" data-go="new-run" type="button">新建估值</button>'));
        return out.join('');
      }

      var hiddenIds = API.hiddenRuns.list();
      out.push(card('运行列表', 'history',
        '<div class="row wrap mb12">' +
        '<span class="badge neutral" id="runCount"></span>' +
        '<button class="btn sm ghost" id="toggleHidden" type="button" hidden></button>' +
        '<button class="btn sm ghost" id="clearHidden" type="button" hidden>全部恢复显示</button>' +
        '<span class="spacer"></span>' +
        '<span class="muted small">隐藏只记在本机浏览器里，runs/ 下的文件一个都不会删</span>' +
        '</div>' +
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>运行编号</th><th>标的</th><th>时间</th><th>状态</th><th>操作</th></tr></thead><tbody id="runRows">' +
        recent.map(function (r) {
          var isCur = r.run_id === cur;
          var isHidden = hiddenIds.indexOf(r.run_id) >= 0;
          return '<tr data-run-row="' + esc(r.run_id) + '"' + (isHidden ? ' data-hidden="1" hidden' : '') +
            (isCur ? ' style="background:rgba(0,229,153,.05)"' : '') + '>' +
            '<td class="mono" data-label="运行编号">' + esc(r.run_id) + (isCur ? ' <span class="badge pass">当前</span>' : '') + '</td>' +
            '<td data-label="标的">' + esc(r.company || '—') + '</td>' +
            '<td class="mono small" data-label="时间">' + esc(F.dt(r.created_at || '')) + '</td>' +
            '<td data-label="状态">' + (r.ok === false ? P.statusBadge('failed') : P.statusBadge('ok')) + '</td>' +
            '<td data-label="操作"><button class="btn sm ghost" data-open-run="' + esc(r.run_id) +
            '" type="button">查看</button> <button class="btn sm ghost" data-hide-run="' + esc(r.run_id) +
            '" type="button">' + (isHidden ? '取消隐藏' : '隐藏') + '</button></td></tr>';
        }).join('') + '</tbody></table></div>', {}));

      var opts = recent.map(function (r, i) {
        return '<option value="' + esc(r.run_id) + '"' + (i === 0 ? ' selected' : '') + '>' + esc(r.run_id) + '</option>';
      }).join('');
      var opts2 = recent.map(function (r, i) {
        return '<option value="' + esc(r.run_id) + '"' + (i === 1 ? ' selected' : (i === 0 && recent.length === 1 ? ' selected' : '')) + '>' + esc(r.run_id) + '</option>';
      }).join('');
      out.push('<div class="grid g-8-4 mt14">' +
        card('两次运行对比', 'filter',
          '<div class="row wrap" style="gap:10px"><div class="field" style="min-width:220px"><label>基准运行</label>' +
          '<select class="input" id="baseRun">' + opts + '</select></div>' +
          '<div class="field" style="min-width:220px"><label>对比运行</label>' +
          '<select class="input" id="targetRun">' + opts2 + '</select></div>' +
          '<button class="btn primary" id="diffBtn" type="button" style="align-self:flex-end">开始对比</button></div>' +
          '<div id="diffOut" class="mt14"><div class="muted small">选择两次运行后点击「开始对比」。</div></div>', {}) +
        card('对比会看什么', 'info',
          '<div class="prose" style="font-size:12.5px">' +
          '<p><b>输入哈希</b>：输入文件是否被改动。<br>' +
          '<b>代码版本</b>：两次运行之间代码是否变了。<br>' +
          '<b>产物哈希</b>：哪些产物逐字节相同、哪些变了。</p>' +
          '<p class="muted small">结论规则：输入未变 + 代码未变 + 产物一致 → 结果稳定；否则定位到具体是哪一层变了。</p></div>', {}) +
        '</div>');
      return out.join('');
    },
    mount: function (root, c) {
      root.querySelectorAll('[data-open-run]').forEach(function (b) {
        b.addEventListener('click', function () { c.setRun(b.getAttribute('data-open-run')); });
      });

      /* 隐藏 / 恢复运行记录（PRD FE-G03：只隐藏不删除） */
      var showHidden = false;
      function rowOf(id) {
        var hit = null;
        root.querySelectorAll('#runRows tr[data-run-row]').forEach(function (tr) {
          if (tr.getAttribute('data-run-row') === id) hit = tr;
        });
        return hit;
      }
      function refreshHiddenUI() {
        var all = root.querySelectorAll('#runRows tr[data-run-row]');
        var hidden = root.querySelectorAll('#runRows tr[data-hidden="1"]');
        var countEl = root.querySelector('#runCount');
        if (countEl) {
          countEl.textContent = (all.length - hidden.length) + ' 条可见' +
            (hidden.length ? '，' + hidden.length + ' 条已隐藏' : '');
        }
        var toggle = root.querySelector('#toggleHidden');
        if (toggle) {
          toggle.hidden = hidden.length === 0;
          toggle.textContent = (showHidden ? '收起已隐藏' : '显示已隐藏') + '（' + hidden.length + '）';
        }
        var clr = root.querySelector('#clearHidden');
        if (clr) clr.hidden = hidden.length === 0;
      }
      root.querySelectorAll('[data-hide-run]').forEach(function (b) {
        b.addEventListener('click', function () {
          var id = b.getAttribute('data-hide-run');
          var tr = rowOf(id);
          var nowHidden = !API.hiddenRuns.has(id);
          if (nowHidden) API.hiddenRuns.add(id); else API.hiddenRuns.remove(id);
          if (tr) {
            if (nowHidden) { tr.setAttribute('data-hidden', '1'); tr.hidden = !showHidden; }
            else { tr.removeAttribute('data-hidden'); tr.hidden = false; }
          }
          b.textContent = nowHidden ? '取消隐藏' : '隐藏';
          c.toast(nowHidden ? ('已隐藏 ' + id) : ('已恢复显示 ' + id));
          refreshHiddenUI();
        });
      });
      var toggle = root.querySelector('#toggleHidden');
      if (toggle) {
        toggle.addEventListener('click', function () {
          showHidden = !showHidden;
          root.querySelectorAll('#runRows tr[data-hidden="1"]').forEach(function (tr) {
            tr.hidden = !showHidden;
          });
          refreshHiddenUI();
        });
      }
      var clr = root.querySelector('#clearHidden');
      if (clr) {
        clr.addEventListener('click', function () {
          root.querySelectorAll('#runRows tr[data-hidden="1"]').forEach(function (tr) {
            var id = tr.getAttribute('data-run-row');
            API.hiddenRuns.remove(id);
            tr.removeAttribute('data-hidden');
            tr.hidden = false;
            var b = tr.querySelector('[data-hide-run]');
            if (b) b.textContent = '隐藏';
          });
          showHidden = false;
          c.toast('已恢复显示全部运行记录');
          refreshHiddenUI();
        });
      }
      refreshHiddenUI();

      var btn = root.querySelector('#diffBtn');
      if (!btn) return;
      btn.addEventListener('click', function () {
        var a = root.querySelector('#baseRun').value, b = root.querySelector('#targetRun').value;
        var out = root.querySelector('#diffOut');
        if (a === b) { out.innerHTML = '<div class="banner warn" style="margin:0">请选择两次不同的运行。</div>'; return; }
        out.innerHTML = '<div class="skel skel-line"></div><div class="skel skel-line" style="width:70%"></div>';
        Promise.all([API.loadRun(a), API.loadRun(b)]).then(function (r) {
          var A = r[0], B = r[1];
          if (!A || !B) { out.innerHTML = '<div class="banner warn" style="margin:0">其中一次运行的产物不完整，无法对比。</div>'; return; }
          function ma(x) {
            var o = {};
            (x.artifacts || []).forEach(function (it) { o[it.file] = it.sha256; });
            return o;
          }
          var A2 = ma(A), B2 = ma(B);
          var all = Object.keys(A2).concat(Object.keys(B2)).filter(function (v, i, s) { return s.indexOf(v) === i; });
          var same = 0, diff = [], onlyA = [], onlyB = [];
          all.forEach(function (k) {
            if (A2[k] && B2[k]) { if (A2[k] === B2[k]) same++; else diff.push(k); }
            else if (A2[k]) onlyA.push(k); else onlyB.push(k);
          });
          var codeA = (A.manifest && A.manifest.inputs && A.manifest.inputs.code) || {};
          var codeB = (B.manifest && B.manifest.inputs && B.manifest.inputs.code) || {};
          var inA = (A.manifest && A.manifest.inputs && A.manifest.inputs.inputs) || {};
          var inB = (B.manifest && B.manifest.inputs && B.manifest.inputs.inputs) || {};
          var inSame = JSON.stringify(inA) === JSON.stringify(inB);
          var codeSame = (codeA.commit === codeB.commit) && (!!codeA.dirty === !!codeB.dirty);
          var verdict = inSame && codeSame && diff.length === 0
            ? ['pass', '输入未变、代码未变，可对比的产物逐字节一致']
            : (!inSame ? ['warn', '两次运行的输入不同，结果差异来自输入'] :
              (!codeSame ? ['warn', '两次运行的代码版本不同，结果差异可能来自代码'] :
                ['warn', '输入与代码一致，但有 ' + diff.length + ' 个产物内容不同（大模型叙述部分天然不可复现）']));
          out.innerHTML =
            '<div class="banner ' + (verdict[0] === 'pass' ? 'info' : 'warn') + '" style="margin:0 0 12px">' +
            Icons.svg(verdict[0] === 'pass' ? 'check' : 'alert') + '<div><b>结论：</b>' + esc(verdict[1]) + '</div></div>' +
            '<div class="grid g-3">' +
            kpi('产物一致', String(same), '个', '', {}) +
            kpi('内容不同', String(diff.length), '个', '', {}) +
            kpi('仅单侧存在', String(onlyA.length + onlyB.length), '个', '', {}) + '</div>' +
            (diff.length ? '<div class="tbl-note">内容不同的产物：<span class="mono">' + esc(diff.slice(0, 12).join('、')) +
              (diff.length > 12 ? ' …' : '') + '</span></div>' : '') +
            '<div class="tbl-note">输入哈希一致：' + (inSame ? '<span class="badge pass">是</span>' : '<span class="badge warn">否</span>') +
            ' ；代码版本一致：' + (codeSame ? '<span class="badge pass">是</span>' : '<span class="badge warn">否</span>') + '</div>';
        }).catch(function () {
          out.innerHTML = '<div class="banner warn" style="margin:0">对比失败：无法读取其中一次运行的清单。</div>';
        });
      });
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 12：设置
     ══════════════════════════════════════════════════════════════════ */
  var settings = {
    id: 'settings', nav: '设置', icon: 'settings', group: '交付与凭证',
    title: '设置与环境自检', sub: function () { return '报告模型、环境依赖与演示偏好'; },
    render: function (c) {
      var d = c.data, st = c.status || {}, env = st.environment || {};
      var nv = (d.manifest && d.manifest.inputs && d.manifest.inputs.narrative) || null;
      var out = [];
      out.push(demoBanner(d));

      var left = [];
      left.push(card('大模型配置', 'sparkles',
        kvRows([
          ['模型', '<span class="mono">' + esc(nv ? (nv.model || '—') : 'deepseek-v4-pro') + '</span>'],
          ['接口地址', '<span class="mono small">' + esc(nv ? (nv.endpoint || '—') : 'https://api.deepseek.com') + '</span>'],
          ['推理强度', '<span class="mono">' + esc(env.effort || 'high') + '</span>'],
          ['温度', nv && nv.temperature != null ? String(nv.temperature) : '0.3'],
          ['Key 来源', '<span class="mono">' + esc(nv && nv.key_source ? nv.key_source : 'DEEPSEEK_API_KEY / apikey.txt') + '</span>'],
          ['Key 状态', env.api_key === false
            ? '<span class="badge warn">未配置</span>'
            : '<span class="badge pass">已配置（界面不显示明文）</span>']
        ]) +
        '<div class="tbl-note">按仓库约定，Key 只通过环境变量或本地 apikey.txt 提供，界面任何位置都不显示明文。</div>',
        { aiIcon: true }));

      var envRows = [
        ['Python', env.python || '—', true, '核心链路只依赖标准库'],
        ['matplotlib', env.matplotlib === false ? '未安装' : (env.matplotlib === true ? '已安装' : '未知'), env.matplotlib !== false,
          '缺少时不生成图表，数据与报告仍然可用'],
        ['reportlab', env.reportlab === false ? '未安装' : (env.reportlab === true ? '已安装' : '未知'), env.reportlab !== false,
          '缺少且无 LaTeX 时不生成 PDF，可用 HTML 打印替代'],
        ['pdfplumber', env.pdfplumber === false ? '未安装' : (env.pdfplumber === true ? '已安装' : '未知'), env.pdfplumber !== false,
          '缺少时无法从年报 PDF 抽取数据'],
        ['LaTeX（xelatex）', env.latex === false ? '未找到' : (env.latex === true ? '已找到' : '未知'), env.latex !== false,
          '缺少时 PDF 退回 reportlab 渲染'],
        ['后端服务', st.running ? '正在运行流水线' : '在线', true, '本地回环，无鉴权']
      ];
      left.push(card('环境自检', 'cpu',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr><th>依赖</th><th>状态</th><th>影响与处理</th></tr></thead><tbody>' +
        envRows.map(function (r) {
          return '<tr><td class="mono" data-label="依赖">' + esc(r[0]) + '</td>' +
            '<td data-label="状态">' + (r[2] ? '<span class="badge pass">' + esc(r[1]) + '</span>'
              : '<span class="badge warn">' + esc(r[1]) + '</span>') + '</td>' +
            '<td class="dim" data-label="影响">' + esc(r[3]) + '</td></tr>';
        }).join('') + '</tbody></table></div>', {}));

      var right = [];
      right.push(card('演示偏好', 'play',
        '<div class="grid" style="gap:9px">' +
        '<label class="row small"><input type="checkbox" id="prefPresent"> 启动时进入演示模式（投影优化）</label>' +
        '<label class="row small" title="需要后端支持参数覆盖运行（API-05）后才能生效">' +
        '<input type="checkbox" id="prefNoLlm" disabled> 演示时默认不调用大模型 ' +
        '<span class="badge neutral">待后端接口</span></label>' +
        '</div>' +
        '<div class="tbl-note">演示模式会放大字号与关键指标、提高对比度；可用快捷键 D 随时切换。' +
        '图表本来就始终显示数值标注，因此没有单独的开关。</div>', {}));

      right.push(card('关于', 'info',
        kvRows([
          ['产品', 'FinAI 自动化估值工作台'],
          ['界面版本', 'v1.0'],
          ['对应文档', 'PRD.md'],
          ['服务端口', String(location.port || '8770')],
          ['界面模式', d.demo ? '演示数据' : '真实产物']
        ]) +
        '<div class="tbl-note">本界面为本地工具，只读 runs/、logs/、data/ 三个目录，' +
        '唯一写操作是触发一次估值运行。</div>', {}));

      right.push(card('快捷键', 'terminal',
        kvRows([
          ['命令面板', '<span class="mono">⌘/Ctrl + K</span>'],
          ['演示模式', '<span class="mono">D</span>'],
          ['运行估值', '<span class="mono">R</span>'],
          ['关闭浮层', '<span class="mono">Esc</span>'],
          ['侧栏折叠', '<span class="mono">[</span>']
        ]), {}));

      out.push('<div class="grid g-7-5">' + '<div class="grid" style="gap:14px">' + left.join('') +
        '</div><div class="grid" style="gap:14px">' + right.join('') + '</div></div>');
      return out.join('');
    },
    mount: function (root) {
      var p = root.querySelector('#prefPresent');
      if (p) {
        p.checked = document.body.classList.contains('present');
        p.addEventListener('change', function () {
          document.body.classList.toggle('present', p.checked);
        });
      }
    }
  };

  /* ══════════════════════════════════════════════════════════════════
     页面 13：取数（定标公司 → 年报）

     这一页回答评审最容易问的那个问题：这些数是从哪来的。
     定标清单 → 年报 PDF → 抽取记录（每个科目带页码与原文）→ 流水线输入 → 行情表校验，
     整条链摊开摆着：一个数字能点到第几页、原文是什么、哪些是倒挤出来的。

     它是**只读**的：下载与抽取要联网、要几分钟，留在命令行里跑。界面上给的是
     「现状 + 下一步敲哪条命令」—— 一个点下去就干等两分钟的按钮，在现场演示里
     是风险，不是功能。
     ══════════════════════════════════════════════════════════════════ */

  /* 仓库内相对路径 → 能直接粘进 cmd 的 Windows 路径 */
  function winPath(p) { return String(p || '').replace(/\//g, '\\'); }

  function recordOfIngest(d, code) {
    var hit = null;
    ((d.records && d.records.items) || []).forEach(function (r) {
      if (!hit && String(r.code) === String(code)) hit = r;
    });
    return hit;
  }

  function hasPdfOf(d, code) {
    return ((d.pdfs && d.pdfs.items) || []).some(function (p) {
      return String(p.code) === String(code);
    });
  }

  function recordAccount(record, field) {
    var hit = null;
    ((record && record.accounts) || []).forEach(function (a) {
      if (!hit && String(a.field) === String(field)) hit = a;
    });
    return hit;
  }

  /* 出处表：一个科目一行 —— 年报直接读到 / 倒挤出来，并给出页码与原文。
     倒挤的字段在年报里没有同名科目，页码那列留空是正常的，不是漏了。
     目标公司有基期资产负债表（11 个科目），可比公司只有 5 个利润类指标，
     两者分开列，免得看的人以为可比公司"少了六个科目"。 */
  function provenanceTable(company) {
    var out = [];
    if ((company.opening || []).length) {
      out.push('<div class="tbl-note">基期资产负债表</div>' +
        rowsTable(company, company.opening));
    }
    if ((company.peer || []).length) {
      out.push('<div class="tbl-note">可比公司指标</div>' +
        rowsTable(company, company.peer, company.peer_values));
    }
    return out.join('');
  }

  /* values：字段 → 数值。抽取记录里没有同名科目时用它兜底 —— 否则会出现
     「出处写着年报直接读取、取值却是空」的画面，看着像漏抽了。 */
  function rowsTable(company, rows, values) {
    values = values || {};
    return '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
      '<th>流水线字段</th><th class="num">取值</th><th>出处</th><th>页码 / 原文</th>' +
      '</tr></thead><tbody>' +
      rows.map(function (row) {
        var acc = recordAccount(company.record, row.field);
        var derived = String(row.provenance).indexOf('derived') === 0;
        var value = (acc && acc.value != null) ? acc.value
          : (values[row.field] != null ? values[row.field] : null);
        return '<tr>' +
          '<td class="mono" data-label="字段">' + esc(row.field) + '</td>' +
          '<td class="num" data-label="取值">' +
          (value != null ? F.money(value) : '<span class="muted">—</span>') + '</td>' +
          '<td data-label="出处">' + (derived
            ? '<span class="badge infer">倒挤</span> <span class="mono small dim">' +
              esc(row.provenance) + '</span>'
            : '<span class="badge pass">年报直接读取</span>') + '</td>' +
          '<td data-label="页码/原文">' + (acc && acc.page != null
            ? '<span class="mono">第 ' + esc(String(acc.page)) + ' 页</span>' +
              (acc.raw ? '<div class="small dim">' + esc(acc.raw) + '</div>' : '')
            : '<span class="muted">—</span>') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function ingestDetail(d) {
    if (!d || !d.available) {
      return P.emptyBlock('alert', '取数层不可用',
        (d && d.error) || '仓库里没有 finai_web 取数层', '');
    }
    var t = d.targets || {}, out = [];
    var records = (d.records && d.records.items) || [];
    var pdfs = (d.pdfs && d.pdfs.items) || [];
    var downloads = (d.downloads && d.downloads.items) || [];
    var input = d.pipeline_input || {};
    var market = d.market || {};

    /* ── 一屏看清现在准备到哪一步 ───────────────────────────────── */
    out.push('<div class="grid g-4">' +
      kpi('定标公司', String(t.count || 0), '家',
        t.exists ? '<span class="muted">' + esc(t.path) + '</span>'
          : '<span class="badge warn">还没有清单</span>', { icon: 'listChecks' }) +
      kpi('年报 PDF', String(pdfs.length), '份',
        '<span class="muted">下载清单 ' + downloads.length + ' 条</span>', { icon: 'download' }) +
      kpi('抽取记录', String(records.length), '家',
        '<span class="muted">' + esc((d.records && d.records.dir) || '') + '</span>', { icon: 'file' }) +
      kpi('行情表', market.ok ? '通过' : (market.exists ? '不通过' : '未提供'), '',
        market.exists ? '<span class="muted">' + esc(market.path) + '</span>'
          : '<span class="badge warn">需要人工导出</span>',
        { hero: true, icon: 'table' }) +
      '</div>');

    /* ── 定标清单 ──────────────────────────────────────────────── */
    if (!t.exists) {
      out.push('<div class="mt14">' + card('定标公司清单', 'listChecks',
        P.emptyBlock('listChecks', '还没有定标公司清单',
          '取数从一份清单开始：把模板复制成 ' + t.path + '，填上要估的公司与年份区间，再跑一次取数。') +
        '<div class="row wrap mt10" style="gap:10px">' +
        '<span class="hash" data-copy="' + esc('copy ' + winPath(t.template) + ' ' + winPath(t.path)) +
        '" title="点击复制命令">copy ' + esc(winPath(t.template)) + ' ' + esc(winPath(t.path)) + '</span>' +
        '<span class="muted small">模板：' + esc(t.template) +
        (t.template_exists ? '' : '（模板文件也缺了）') + '</span>' +
        '</div>', {}) + '</div>');
    } else {
      out.push('<div class="mt14">' + card('定标公司清单', 'listChecks',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>代码</th><th>名称</th><th>年份</th><th>年报 PDF</th><th>抽取</th><th>方法</th>' +
        '</tr></thead><tbody>' +
        (t.items || []).map(function (item) {
          var rec = recordOfIngest(d, item.ticker);
          return '<tr>' +
            '<td class="mono" data-label="代码">' + esc(item.ticker) + '</td>' +
            '<td data-label="名称">' + esc(item.name || '—') + '</td>' +
            '<td class="mono small" data-label="年份">' +
            esc((item.years || []).join(' / ') || '—') + '</td>' +
            '<td data-label="年报 PDF">' + (hasPdfOf(d, item.ticker)
              ? '<span class="badge pass">已入库</span>'
              : '<span class="badge neutral">未下载</span>') + '</td>' +
            '<td data-label="抽取">' + (rec
              ? '<span class="badge pass">' + F.int(rec.account_count) + ' 个科目</span>'
              : '<span class="badge neutral">未抽取</span>') + '</td>' +
            '<td class="small dim" data-label="方法">' + esc((rec && rec.method) || '—') + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table></div>' +
        (t.error ? '<div class="banner warn" style="margin-top:10px">' + Icons.svg('alert') +
          '<div>清单读取有问题：' + esc(t.error) + '</div></div>' : '') +
        '<div class="tbl-note">共 ' + (t.items || []).length +
        ' 家。年报通常在次年披露，年份区间要写披露年份所在的那一年。</div>', {}) + '</div>');
    }

    /* ── 数值出处：每个数字的页码与原文 ────────────────────────── */
    if (input.exists && (input.companies || []).length) {
      var blocks = (input.companies || []).map(function (company) {
        company.record = recordOfIngest(d, company.code);
        return '<details class="acc" open><summary>' +
          '<span class="badge neutral">' + esc(company.code) + '</span> ' +
          '<b>' + esc((company.record && company.record.name) || company.code) + '</b> ' +
          '<span class="muted small">' + esc(company.period || '') +
          ' · 方法 ' + esc(company.method || '—') + '</span></summary>' +
          (company.sha256 || company.source_url
            ? '<div class="row wrap small" style="gap:10px;margin-bottom:8px">' +
              (company.sha256 ? '<span class="muted">PDF sha256</span>' + hashChip(company.sha256) : '') +
              (company.source_url ? '<span class="muted">来源</span>' +
                '<span class="mono small">' + esc(company.source_url) + '</span>' : '') +
              '</div>' : '') +
          provenanceTable(company) +
          ((company.notes || []).length ? '<div class="tbl-note">' +
            company.notes.map(function (n) { return '· ' + esc(n); }).join('<br>') + '</div>' : '') +
          '</details>';
      }).join('');
      out.push('<div class="mt14">' + card('这些数字是从哪来的', 'file',
        '<div class="row wrap" style="gap:10px;margin-bottom:10px">' +
        '<span class="badge neutral">' + esc(input.path) + '</span>' +
        '<span class="muted small">生成于 ' + esc(F.dt(input.generated_at) || '—') +
        '；「倒挤」= 年报里没有同名科目，由其他科目相减得到</span></div>' +
        blocks, {}) + '</div>');
    } else {
      out.push('<div class="mt14">' + card('这些数字是从哪来的', 'file',
        P.emptyBlock('file', '还没有「年报 → 流水线输入」这一步',
          '跑完取数后，这里会列出每个科目的出处：年报第几页、原文是什么、哪些是倒挤出来的。') +
        '<div class="row wrap mt10" style="gap:10px">' +
        '<span class="hash" data-copy="python -m finai_web run" title="点击复制">' +
        'python -m finai_web run</span>' +
        '<span class="muted small">联网下载年报并抽取。只想先看效果，可以跑离线样例 ' +
        '<span class="mono">python finai_web/examples/offline_demo.py</span></span>' +
        '</div>', {}) + '</div>');
    }

    /* ── 行情表校验 ────────────────────────────────────────────── */
    var marketBody;
    if (!market.exists) {
      marketBody = P.emptyBlock('table', '还没有行情表',
        '股价、摊薄股数与有息负债是终端数据，程序不生成，要从 Wind / Choice 导出到 ' +
        market.path + '。') +
        '<div class="row wrap mt10" style="gap:10px">' +
        '<span class="hash" data-copy="python -m finai_web market --template" title="点击复制">' +
        'python -m finai_web market --template</span></div>';
    } else {
      marketBody = kvRows([
        ['结论', market.ok
          ? '<span class="badge pass">通过</span>'
          : '<span class="badge fail">不通过</span>'],
        ['数据行', F.int(market.rows) + ' 行'],
        ['目标 / 可比', esc(market.target || '—') + ' / ' + F.int(market.peer_count) + ' 家']
      ]) +
      ((market.errors || []).length
        ? '<div class="banner fail" style="margin-top:10px">' + Icons.svg('alert') +
          '<div>' + market.errors.map(function (e) { return '· ' + esc(e); }).join('<br>') +
          '</div></div>' : '') +
      ((market.warnings || []).length
        ? '<div class="tbl-note">' +
          market.warnings.map(function (w) { return '· ' + esc(w); }).join('<br>') + '</div>' : '') +
      (market.error ? '<div class="tbl-note">' + esc(market.error) + '</div>' : '');
    }
    out.push('<div class="mt14">' + card('行情表校验', 'table', marketBody, {}) + '</div>');

    /* ── 下载清单：来源 URL 与哈希（数据来源可核验的凭证） ─────── */
    if (downloads.length) {
      out.push('<div class="mt14">' + card('年报下载清单', 'download',
        '<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>代码</th><th>年份</th><th>来源</th><th>sha256</th><th class="num">大小</th>' +
        '</tr></thead><tbody>' +
        downloads.map(function (item) {
          return '<tr>' +
            '<td class="mono" data-label="代码">' + esc(item.code) + '</td>' +
            '<td class="mono" data-label="年份">' +
            esc(String(item.year == null ? '—' : item.year)) + '</td>' +
            '<td class="small" data-label="来源">' + (item.url
              ? '<a class="mono small" href="' + esc(item.url) +
                '" target="_blank" rel="noopener">' + esc(F.short(item.url, 46)) + '</a>'
              : '<span class="muted">—</span>') + '</td>' +
            '<td data-label="sha256">' + hashChip(item.sha256) + '</td>' +
            '<td class="num" data-label="大小">' +
            (item.bytes ? F.moneyShort(item.bytes) + ' ' + F.moneyUnit(item.bytes) : '—') +
            '</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<div class="tbl-note">这份清单就是「数据来源可核验」的凭证：来源 URL + 文件哈希，' +
        '报告附录里的出处指向同一份记录。</div>', {}) + '</div>');
    }

    return out.join('');
  }

  /* ══════════════════════════════════════════════════════════════════
     取数向导：输入公司名 → 自动取数 → 复核假设 → 估值

     为什么要把「复核假设」卡在中间：资本开支口径这类判断会让结论从 +76 元
     翻到 −10 元（长春高新实测），不能由程序替人做。取数算出来的每个值都带
     状态标签，人就着标签复核一遍再往下走。
     ══════════════════════════════════════════════════════════════════ */
  var WIZ = {
    target: null, peers: [], candidates: [], peerCandidates: [],
    company: '', draft: null, asOf: '', busy: '', error: '', note: '', companies: []
  };

  var FIELD_STATUS = {
    derived: ['自动推导', 'pass'],
    default: ['规则默认', 'infer'],
    kept: ['沿用上一版', 'neutral'],
    missing: ['需人工填', 'warn'],
    human: ['人工确认', 'pass']
  };

  function statusChip(status) {
    var item = FIELD_STATUS[status] || [String(status), 'neutral'];
    return '<span class="badge ' + item[1] + '">' + esc(item[0]) + '</span>';
  }

  function nameOf(item) {
    return esc(item.name || item.code) + ' <span class="mono small dim">' + esc(item.code) + '</span>';
  }

  function candidateChips(items, action, label) {
    if (!items.length) return '<span class="muted small">没有候选</span>';
    return items.map(function (item) {
      return '<button class="badge code" type="button" data-wiz="' + action + '" data-code="' + esc(item.code) +
        '" data-name="' + esc(item.name || '') + '">' + Icons.svg('plus') + nameOf(item) +
        (label ? ' <span class="dim">' + esc(label) + '</span>' : '') + '</button>';
    }).join(' ');
  }

  function wizardTargetBlock() {
    if (WIZ.target) {
      return '<div class="row wrap" style="gap:10px;align-items:center">' +
        '<span class="badge pass">' + nameOf(WIZ.target) + '</span>' +
        '<span class="muted small">' + esc((WIZ.target.years || []).join('、') || '年份待定') +
        ' 年披露的年报</span>' +
        '<button class="ghost" type="button" data-wiz="clear-target">换一家</button></div>';
    }
    return '<div class="row wrap" style="gap:8px;align-items:center">' +
      '<input class="input mono" style="min-width:220px" data-wiz-input="target" placeholder="公司名或六位代码，例如 长春高新 / 600519">' +
      '<button class="primary" type="button" data-wiz="search-target">' + Icons.svg('search') + '检索</button>' +
      '<span class="muted small">支持模糊：只输入「长春」会列出相关的多家公司让你选</span></div>' +
      (WIZ.candidates.length
        ? '<div class="mt10">' + candidateChips(WIZ.candidates, 'pick-target') + '</div>'
        : '');
  }

  function wizardPeerBlock() {
    var chosen = WIZ.peers.length
      ? WIZ.peers.map(function (peer) {
        return '<span class="badge neutral">' + nameOf(peer) +
          ' <button class="badge code" type="button" data-wiz="remove-peer" data-code="' + esc(peer.code) + '">×</button></span>';
      }).join(' ')
      : '<span class="badge warn">还没有可比公司</span>';
    return '<div class="row wrap" style="gap:10px;align-items:center">' + chosen + '</div>' +
      '<div class="row wrap mt10" style="gap:8px;align-items:center">' +
      '<input class="input mono" style="min-width:220px" data-wiz-input="peer" placeholder="可比公司：名称或代码">' +
      '<button class="ghost" type="button" data-wiz="add-peer">' + Icons.svg('plus') + '添加可比公司</button>' +
      '<span class="muted small">2–4 家为宜（太少没有可比性，太多口径难对齐）</span></div>' +
      (WIZ.peerCandidates.length
        ? '<div class="mt10">' + candidateChips(WIZ.peerCandidates, 'pick-peer') + '</div>'
        : '');
  }

  function draftFieldRow(key, item) {
    var value = item.value;
    var control;
    if (typeof value === 'boolean') {
      control = '<select class="input" data-draft="' + esc(key) + '" data-type="bool">' +
        '<option value="true"' + (value ? ' selected' : '') + '>true</option>' +
        '<option value="false"' + (value ? '' : ' selected') + '>false</option></select>';
    } else if (Array.isArray(value)) {
      control = '<input class="input mono" data-draft="' + esc(key) + '" data-type="list" value="' +
        esc(value.join(', ')) + '">';
    } else if (value == null) {
      control = '<input class="input mono" data-draft="' + esc(key) + '" data-type="num" value="" placeholder="待填">';
    } else if (typeof value === 'number') {
      control = '<input class="input mono" data-draft="' + esc(key) + '" data-type="num" value="' + value + '">';
    } else {
      control = '<input class="input mono" data-draft="' + esc(key) + '" data-type="text" value="' + esc(value) + '">';
    }
    var short = key.indexOf('.') > 0 ? key.split('.')[1] : key;
    return '<tr>' +
      '<td class="mono small" data-label="字段">' + esc(short) + '<div class="small dim">' + esc(key) + '</div></td>' +
      '<td data-label="取值">' + control + '</td>' +
      '<td data-label="状态">' + statusChip(item.status) + '</td>' +
      '<td class="small dim" data-label="出处">' + esc(item.source || '') + '</td>' +
      '</tr>';
  }

  function wizardDraftBlock() {
    if (!WIZ.draft) return '';
    var groups = WIZ.draft.groups || {};
    var order = ['derived', 'default', 'kept', 'missing', 'human'];
    var titles = {
      derived: '自动推导（从年报或行情算出来的，附出处）',
      default: '规则默认（保守规则给的，请复核 —— 资本开支口径尤其要看）',
      kept: '沿用上一版（这次算不出来）',
      missing: '需人工填（年报里没有这个数，程序不猜）',
      human: '人工确认（改过的值）'
    };
    var out = [];
    if (WIZ.draft.normalized && WIZ.draft.normalized.applied) {
      out.push('<div class="alert warn">归一建议：' + esc(WIZ.draft.normalized.reason) + '</div>');
    }
    (WIZ.draft.notes || []).forEach(function (note) {
      out.push('<div class="tbl-note">' + esc(note) + '</div>');
    });
    order.forEach(function (status) {
      var items = groups[status] || [];
      if (!items.length) return;
      out.push('<div class="tbl-note mt14">' + esc(titles[status] || status) + ' · ' + items.length + ' 项</div>');
      out.push('<div class="tbl-wrap"><table class="tbl responsive"><thead><tr>' +
        '<th>字段</th><th>取值</th><th>状态</th><th>出处</th></tr></thead><tbody>' +
        items.map(function (item) { return draftFieldRow(item.key, item); }).join('') +
        '</tbody></table></div>');
    });
    var missing = (WIZ.draft.missing || []);
    out.push('<div class="row wrap mt14" style="gap:10px;align-items:center">' +
      '<button class="primary" type="button" data-wiz="run-valuation"' + (missing.length ? ' disabled' : '') + '>' +
      Icons.svg('play') + '确认并估值</button>' +
      '<button class="ghost" type="button" data-wiz="save-draft">' + Icons.svg('save') + '只保存</button>' +
      (missing.length
        ? '<span class="badge warn">还有 ' + missing.length + ' 项待填：' + esc(missing.join('、')) + '</span>'
        : '<span class="muted small">确认无误后点「确认并估值」</span>') +
      '</div>');
    out.push('<div class="tbl-note">落盘位置：' + esc(WIZ.draft.assumptions_path || '') +
      '（覆盖前会自动备份成 assumptions.&lt;时间戳&gt;.bak，人写的依据条目原样保留）</div>');
    return out.join('');
  }

  function wizardHtml(c) {
    var st = (c && c.status) || {};
    var running = !!st.running;
    var phase = st.phase || '';
    var steps = (st.steps || []).length;
    var out = [];
    out.push('<div class="grid g-4">' +
      kpi('目标公司', WIZ.target ? esc(WIZ.target.name || WIZ.target.code) : '—', '',
        WIZ.target ? '<span class="muted">' + esc(WIZ.target.code) + '</span>'
          : '<span class="badge warn">先输入公司名</span>', { icon: 'target' }) +
      kpi('可比公司', String(WIZ.peers.length), '家',
        WIZ.peers.length
          ? '<span class="muted">' + esc(WIZ.peers.map(function (p) { return p.name || p.code; }).join('、')) + '</span>'
          : '<span class="badge warn">至少选一家</span>', { icon: 'layers' }) +
      kpi('假设草稿', WIZ.draft ? '已生成' : '—', '',
        WIZ.draft
          ? '<span class="muted">' + String((WIZ.draft.missing || []).length) + ' 项待填</span>'
          : '<span class="muted">取数后生成</span>', { icon: 'listChecks' }) +
      kpi('当前阶段', running ? (phase === 'plan' ? '取数中' : '估值中') : '就绪', '',
        '<span class="muted">' + (running ? '已执行 ' + steps + ' 步' : '等待操作') + '</span>',
        { hero: true, icon: 'activity' }) +
      '</div>');

    if (WIZ.companies.length) {
      out.push('<div class="mt14"><div class="tbl-note">已经取过数的公司（点一下载入它的假设草稿，直接跑估值）：</div>' +
        '<div class="row wrap mt10" style="gap:8px">' +
        WIZ.companies.map(function (item) {
          return '<button class="badge ' + (item.has_draft ? 'code' : 'neutral') + '" type="button" data-wiz="load-company" data-code="' +
            esc(item.code) + '">' + esc(item.name || item.code) + ' <span class="mono small dim">' + esc(item.code) +
            '</span>' + (item.has_draft ? '' : ' <span class="dim">（还没取数）</span>') + '</button>';
        }).join('') + '</div></div>');
    }

    out.push('<div class="mt14">' + card('① 要估哪家公司', 'target',
      wizardTargetBlock() +
      '<div class="tbl-note mt10">输入名称后程序去巨潮资讯网检索：代码、交易所板块、机构号都会自动填好，' +
      '再据此下载最近两份年报（当年那份给基期资产负债表，上一份给利润表 —— 归一盈利要用）。</div>', {}) + '</div>');

    out.push(card('② 可比公司', 'layers', wizardPeerBlock() +
      '<div class="tbl-note mt10">相对估值要用它们的市值与倍数的，所以要跟目标公司是同一门生意、同一套口径。</div>', {}));

    var missingPeer = !WIZ.peers.length;
    out.push(card('③ 取数并生成假设', 'database',
      '<div class="row wrap" style="gap:10px;align-items:center">' +
      '<button class="primary" type="button" data-wiz="start-plan"' +
      (!WIZ.target || running ? ' disabled' : '') + '>' + Icons.svg('download') + '开始取数</button>' +
      '<span class="muted small">' +
      '会依次做六件事：解析公司 → 下载年报 → 抽取科目 → 汇总 → 取股价 → 生成假设草稿' +
      '（首次约两分钟，要联网；已经下过的年报不会重复下）</span></div>' +
      (WIZ.error ? '<div class="alert mt10">' + esc(WIZ.error) + '</div>' : '') +
      (WIZ.note ? '<div class="tbl-note mt10">' + esc(WIZ.note) + '</div>' : '') +
      (running
        ? '<div class="mt14 tbl-note">正在跑：' + esc(phase === 'plan' ? '取数' : '估值') +
        '（任务号 ' + esc(st.job_id || st.run_id || '') + '）。页面会自动刷新。</div>'
        : '') +
      (missingPeer ? '<div class="tbl-note mt10">提示：没有可比公司相对估值做不了，取数会在汇总那一步停下。</div>' : ''),
      {}));

    if (WIZ.draft) {
      out.push(card('④ 复核假设（这一步是给人看的）', 'listChecks', wizardDraftBlock(), {}));
    }
    return out.join('');
  }

  function draftFieldsFromDom(root) {
    var fields = {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-draft]'), function (el) {
      var key = el.getAttribute('data-draft');
      var type = el.getAttribute('data-type');
      var raw = el.value;
      if (type === 'bool') { fields[key] = raw === 'true'; return; }
      if (type === 'list') {
        fields[key] = raw.split(/[,，\s]+/).filter(Boolean).map(Number);
            return;
          }
      fields[key] = raw;
    });
    return fields;
  }

  /* 只重画向导那块，不惊动整页 —— 整页重绘归 app.js 的 paint 管，
     这里改了 WIZ 的状态就重画自己，输入框之外的东西都不动。 */
  function repaintWizard(root, c) {
    var box = root.querySelector('#ingestWizard');
    if (box) box.innerHTML = wizardHtml(c);
  }

  function loadDraft(code) {
    WIZ.company = code;
    return API.planDraft(code).then(function (r) {
      if (!r.ok) { WIZ.draft = null; WIZ.error = (r.body && r.body.error) || '读不到假设草稿'; return; }
      WIZ.draft = r.body;
      if (r.body.company_info) WIZ.target = { code: r.body.company_info.code, name: r.body.company_info.name };
      if (r.body.company && r.body.company.peers) {
        WIZ.peers = r.body.company.peers.map(function (item) { return { code: item, name: '' }; });
      }
      WIZ.error = '';
    });
  }

  function mountWizard(root, c) {
    if (!root) return;
    if (!WIZ.companies.length) {
      API.companies().then(function (r) {
        if (r.ok && r.body.companies) {
          WIZ.companies = r.body.companies.filter(function (item) { return item.has_profile; });
          if (WIZ.companies.length) repaintWizard(root, c);
        }
      });
    }
    root.addEventListener('click', function (event) {
      var el = event.target.closest('[data-wiz]');
      if (!el) return;
      var action = el.getAttribute('data-wiz');
      var code = el.getAttribute('data-code') || '';
      var name = el.getAttribute('data-name') || '';
      var input;
      if (action === 'search-target') {
        input = root.querySelector('[data-wiz-input="target"]');
        WIZ.busy = 'search'; WIZ.error = '';
        API.resolveName(input.value).then(function (r) {
          WIZ.busy = '';
          if (!r.ok) { WIZ.error = (r.body && (r.body.error || r.body.hint)) || '检索失败'; repaintWizard(root, c); return; }
          WIZ.candidates = (r.body.candidates || []);
          if (!WIZ.candidates.length) WIZ.error = r.body.hint || '没有查到这家公司';
          repaintWizard(root, c);
        });
      } else if (action === 'pick-target') {
        WIZ.target = { code: code, name: name };
        WIZ.candidates = [];
        WIZ.error = '';
        repaintWizard(root, c);
      } else if (action === 'clear-target') {
        WIZ.target = null; repaintWizard(root, c);
      } else if (action === 'add-peer') {
        input = root.querySelector('[data-wiz-input="peer"]');
        WIZ.busy = 'peer'; WIZ.error = '';
        API.resolveName(input.value).then(function (r) {
          WIZ.busy = '';
          var items = (r.body && r.body.candidates) || [];
          if (!r.ok || !items.length) { WIZ.error = (r.body && (r.body.error || r.body.hint)) || '没查到这家公司'; repaintWizard(root, c); return; }
          if (items.length === 1) { WIZ.peers.push({ code: items[0].code, name: items[0].name }); WIZ.peerCandidates = []; }
          else WIZ.peerCandidates = items;
          repaintWizard(root, c);
        });
      } else if (action === 'pick-peer') {
        WIZ.peers.push({ code: code, name: name });
        WIZ.peerCandidates = [];
        repaintWizard(root, c);
      } else if (action === 'remove-peer') {
        WIZ.peers = WIZ.peers.filter(function (peer) { return peer.code !== code; });
        repaintWizard(root, c);
      } else if (action === 'load-company') {
        loadDraft(code).then(function () { repaintWizard(root, c); });
      } else if (action === 'start-plan') {
        WIZ.error = ''; WIZ.note = '';
        API.startPlan({
          target_code: WIZ.target.code, keyword: WIZ.target.name || WIZ.target.code,
          peers: WIZ.peers.map(function (peer) { return peer.code; })
        }).then(function (r) {
          if (!r.ok) { WIZ.error = (r.body && r.body.error) || '起取数任务失败'; repaintWizard(root, c); return; }
          WIZ.company = WIZ.target.code;
          WIZ.note = '取数任务已开始（' + r.body.job_id + '）。跑完会自动载入假设草稿。';
          repaintWizard(root, c);
        });
      } else if (action === 'save-draft') {
        API.saveDraft(WIZ.company, draftFieldsFromDom(root)).then(function (r) {
          if (!r.ok) { WIZ.error = (r.body && r.body.error) || '保存失败'; repaintWizard(root, c); return; }
          WIZ.note = '已保存到假设草稿。';
          loadDraft(WIZ.company).then(function () { repaintWizard(root, c); });
        });
      } else if (action === 'run-valuation') {
        API.saveDraft(WIZ.company, draftFieldsFromDom(root)).then(function (saved) {
          if (!saved.ok) { WIZ.error = (saved.body && saved.body.error) || '保存失败'; repaintWizard(root, c); return; }
          API.startIngestRun(WIZ.company).then(function (r) {
            if (!r.ok) { WIZ.error = (r.body && r.body.error) || '起估值任务失败'; repaintWizard(root, c); return; }
            WIZ.note = '估值已开始（运行编号 ' + r.body.run_id + '）。';
            repaintWizard(root, c);
          });
        });
      }
    });

    /* 取数跑完自动载入草稿：状态从「运行中」变成「结束」时载一次 */
    var status = (c && c.status) || {};
    var stopped = WIZ.wasRunning && !status.running;
    WIZ.wasRunning = !!status.running;
    if (stopped && status.phase === 'plan') {
      if (status.job_returncode === 0 && WIZ.target) {
        loadDraft(WIZ.target.code).then(function () { repaintWizard(root, c); });
      } else if (status.error) {
        WIZ.error = status.error;
        repaintWizard(root, c);
      }
    }
  }
  var ingest = {
    id: 'ingest', nav: '取数', icon: 'database', group: '数据准备',
    title: '取数（定标公司 → 年报）',
    sub: function () { return '输入公司名 → 年报 → 结构化科目 → 假设草稿 → 估值'; },
    render: function (c) {
      var src = (c.status && c.status.data_source) || {};
      var sourceValue = src.switch === 'extracted'
        ? '<span class="badge pass">年报抽取结果</span>'
        : (src.switch ? '<span class="badge neutral">手填过渡数据</span>'
          : '<span class="muted">还没有运行记录</span>');
      var out = [];
      out.push(demoBanner(c.data));
      out.push(card('本次运行的数据来源', 'database',
        '<div class="grid g-6-6">' +
        kvRows([
          ['数据源', sourceValue],
          ['基期资产负债表', src.opening_balance
            ? esc(src.opening_balance) : '<span class="muted">—</span>'],
          ['可比公司财务', src.peers ? esc(src.peers) : '<span class="muted">—</span>']
        ]) +
        '<div class="tbl-note" style="margin-top:10px">切到年报抽取结果：把环境变量设为 ' +
        '<span class="hash" data-copy="FINAI_DATA_SOURCE=extracted" title="点击复制">' +
        'FINAI_DATA_SOURCE=extracted</span>，再跑 <span class="mono">python -m pipeline.run</span>。' +
        '不设置时读 <span class="mono">data/assumptions.toml</span> 与 ' +
        '<span class="mono">data/peers.csv</span>。下面的向导会替你跑完整条链路：' +
        '按名称检索公司 → 下载年报 → 抽取 → 生成假设草稿 → 等你确认 → 估值。</div></div>', {}));
      out.push('<div id="ingestWizard" class="mt14">' + wizardHtml(c) + '</div>');
      out.push('<div id="ingestDetail" class="mt14">' +
        '<div class="skel skel-card" style="height:160px"></div></div>');
      return out.join('');
    },
    mount: function (root, c) {
      mountWizard(root, c);
      var box = root.querySelector('#ingestDetail');
      if (!box) return;
      API.get('/api/ingest').then(function (d) {
        box.innerHTML = ingestDetail(d);
      }).catch(function (err) {
        box.innerHTML = P.emptyBlock('alert', '取数层信息读不到',
          '本地服务没有返回 /api/ingest：' + String((err && err.message) || err), '');
      });
    }
  };

  P.list = P.list.concat([comps, validation, report, evidence, runs, settings]);
  /* 「取数」排在导航最前面：它是整条链路的第一步（取数 → 估值 → 分析 → 交付）。
     页面定义在本文件里，所以用 unshift，而不是回头改 pages.js 的数组字面量。 */
  P.list.unshift(ingest);

  P.md2html = md2html;
})(window);
