/* 演示数据集（内置样例）
   ----------------------------------------------------------------------
   用途：当本地没有任何运行产物时，界面仍然可用、可演示。
   边界：这里的所有数字都是**为演示虚构的**，与真实运行无关。
        真实运行时，页面一律读取 runs/<run_id>/ 下的产物文件，
        不会调用本文件里的任何计算（PRD 第 3.2 节：前端不得自行推导估值数字）。
   单位：与后端保持一致 —— 金额＝元，股数＝股，比率＝小数。
   ---------------------------------------------------------------------- */
(function (global) {
  'use strict';

  var E8 = 1e8;   // 亿元 → 元

  /* ── 演示用假设（对齐仓库 data/assumptions.toml 的结构） ───────────── */
  var ASSUMPTIONS = {
    company: {
      name: '示例科技（演示）',
      code: 'DEMO.SZ',
      source: '内置演示样本'
    },
    base_year: 2025,
    drivers: {
      revenue_base: 1360.15 * E8,
      revenue_growth: [0.162, 0.151, 0.141, 0.121, 0.111],
      ebit_margin: [0.305, 0.310, 0.315, 0.318, 0.320],
      gross_margin: 0.452,
      opex_pct_revenue: 0.147,
      da_pct_revenue: 0.052,
      capex_pct_revenue: 0.068,
      nwc_pct_revenue: 0.110,
      tax_rate: 0.25
    },
    terminal: {
      terminal_method: 'perpetuity',
      terminal_growth: 0.025,
      exit_multiple: 8.5,
      mid_year: false
    },
    wacc_parts: {
      risk_free: 0.0235,
      beta: 1.12,
      equity_risk_premium: 0.0625,
      cost_of_debt: 0.0385,
      tax_rate: 0.25,
      equity_weight: 0.86,
      debt_weight: 0.14
    },
    market: {
      price: 288.00,
      diluted_shares: 2.57e9,
      interest_bearing_debt: 320.0 * E8,
      cash: 505.2 * E8,
      minority_interest: 0,
      preferred_stock: 0,
      non_operating_assets: 0,
      as_of: '2026-09-30'
    },
    sensitivity: {
      wacc_delta: 0.01,
      terminal_growth_delta: 0.01,
      revenue_growth_delta: 0.03,
      ebit_margin_delta: 0.03,
      tax_rate_delta: 0.05,
      grid_points: 5
    },
    evidence: {
      'drivers.revenue_growth': '基期收入与逐年增速为演示样本值；⚠️ 待替换为年报披露的历史增速与管理层指引',
      'drivers.ebit_margin': 'EBIT 利润率区间取自演示样本；⚠️ 待替换为最近三年经营数据',
      'drivers.tax_rate': '25% 为法定税率，未考虑高新技术等优惠；⚠️ 待核实公司实际适用税率',
      'wacc.risk_free': '2.35% 取自中国 10 年期国债到期收益率；⚠️ 待填基准日',
      'wacc.beta': '1.12 为演示值；⚠️ 待查：对哪个指数、什么频率、多长窗口',
      'wacc.equity_risk_premium': '6.25% 为演示值；⚠️ 待查：引用哪份研究、哪一版',
      'wacc.cost_of_debt': '3.85% 为演示值；⚠️ 待替换为「利息支出 / 平均有息负债」，取自年报附注',
      'terminal.terminal_growth': '2.5% 低于长期名义 GDP 增速，属于保守取值；⚠️ 待补充引用来源',
      'market.price': '目标公司股价与股数取自演示样本，基准日 2026-09-30；⚠️ 待核对该日收盘价'
    },
    opening_balance: {
      cash: 505.2 * E8,
      accounts_receivable: 268.4 * E8,
      inventory: 314.0 * E8,
      fixed_assets: 682.6 * E8,
      other_non_current_assets: 190.0 * E8,
      accounts_payable: 291.4 * E8,
      interest_bearing_debt: 320.0 * E8,
      other_non_current_liabilities: 88.0 * E8,
      share_capital: 25.7 * E8,
      retained_earnings: 1234.9 * E8,
      minority_equity: 1.2 * E8
    }
  };

  /* ── 演示用最小估值内核（仅用于生成演示数据） ─────────────────────── */
  function project(a, baseYear) {
    var d = a.drivers, out = { years: [], revenue: [], ebit: [], da: [], ebitda: [],
      capex: [], nwc: [], delta_nwc: [], nopat: [], fcff: [] };
    var rev = d.revenue_base;
    var nwcPrev = rev * d.nwc_pct_revenue;
    for (var i = 0; i < d.revenue_growth.length; i++) {
      rev = rev * (1 + d.revenue_growth[i]);
      var ebit = rev * d.ebit_margin[i];
      var da = rev * d.da_pct_revenue;
      var capex = rev * d.capex_pct_revenue;
      var nwc = rev * d.nwc_pct_revenue;
      var dnwc = nwc - nwcPrev;
      var nopat = ebit * (1 - d.tax_rate);
      out.years.push(baseYear + 1 + i);
      out.revenue.push(rev);
      out.ebit.push(ebit);
      out.da.push(da);
      out.ebitda.push(ebit + da);
      out.capex.push(capex);
      out.nwc.push(nwc);
      out.delta_nwc.push(dnwc);
      out.nopat.push(nopat);
      out.fcff.push(nopat + da - capex - dnwc);
      nwcPrev = nwc;
    }
    return out;
  }

  function value(a, wacc, overrides) {
    var d = Object.assign({}, a.drivers, overrides || {});
    var g = (overrides && overrides.terminal_growth != null)
      ? overrides.terminal_growth : a.terminal.terminal_growth;
    var w = (overrides && overrides.wacc != null) ? overrides.wacc : wacc;
    var model = { drivers: d, terminal: a.terminal };
    var p = project(model, a.base_year);
    var df = [], pv = [], n = p.fcff.length;
    for (var i = 0; i < n; i++) {
      var f = 1 / Math.pow(1 + w, i + 1);
      df.push(f); pv.push(p.fcff[i] * f);
    }
    var tv = (a.terminal.terminal_method === 'exit_multiple')
      ? p.ebitda[n - 1] * (a.terminal.exit_multiple || 1)
      : p.fcff[n - 1] * (1 + g) / (w - g);
    var pvTv = tv * df[n - 1];
    var sumPv = pv.reduce(function (s, x) { return s + x; }, 0);
    var m = a.market;
    var ev = sumPv + pvTv;
    var equity = ev - m.interest_bearing_debt + m.cash - m.minority_interest
      - m.preferred_stock + m.non_operating_assets;
    var per = equity / m.diluted_shares;
    return {
      wacc: w, projection: p, discount_factor: df, pv_fcff: pv,
      terminal_value: tv, pv_terminal: pvTv, enterprise_value: ev,
      net_debt: m.interest_bearing_debt - m.cash, equity_value: equity,
      diluted_shares: m.diluted_shares, per_share: per, price: m.price,
      implied_return: per / m.price - 1,
      terminal_share_of_ev: pvTv / ev,
      implied_exit_multiple: ev / p.ebitda[n - 1]
    };
  }

  function avg(arr) { return arr.reduce(function (s, x) { return s + x; }, 0) / arr.length; }

  function broadcast(delta, len) {
    var out = [];
    for (var i = 0; i < len; i++) out.push(delta);
    return out;
  }

  function torn(a, wacc) {
    var s = a.sensitivity, d = a.drivers, n = d.revenue_growth.length;
    var base = avg(d.revenue_growth), margin = avg(d.ebit_margin);
    var defs = [
      { name: 'WACC', field: 'wacc', base: wacc,
        lo: wacc - s.wacc_delta, hi: wacc + s.wacc_delta,
        apply: function (v) { return { wacc: v }; } },
      { name: '永续增长率 g', field: 'terminal_growth',
        base: a.terminal.terminal_growth,
        lo: a.terminal.terminal_growth - s.terminal_growth_delta,
        hi: a.terminal.terminal_growth + s.terminal_growth_delta,
        apply: function (v) { return { terminal_growth: v }; } },
      { name: '收入增速', field: 'revenue_growth', base: base,
        lo: base - s.revenue_growth_delta,
        hi: base + s.revenue_growth_delta,
        apply: function (v) { return { revenue_growth: broadcast(v, n) }; } },
      { name: 'EBIT 利润率', field: 'ebit_margin', base: margin,
        lo: margin - s.ebit_margin_delta,
        hi: margin + s.ebit_margin_delta,
        apply: function (v) { return { ebit_margin: broadcast(v, n) }; } },
      { name: '所得税率', field: 'tax_rate', base: d.tax_rate,
        lo: d.tax_rate - s.tax_rate_delta,
        hi: d.tax_rate + s.tax_rate_delta,
        apply: function (v) { return { tax_rate: v }; } }
    ];
    var baseVal = value(a, wacc).per_share;
    var rows = defs.map(function (def) {
      var lo = value(a, wacc, def.apply(def.lo)).per_share;
      var hi = value(a, wacc, def.apply(def.hi)).per_share;
      return {
        name: def.name, field: def.field, low: def.lo, base: def.base, high: def.hi,
        per_share_at_low: lo, per_share_at_high: hi, per_share_base: baseVal,
        swing: Math.abs(hi - lo)
      };
    });
    rows.sort(function (x, y) { return y.swing - x.swing; });
    return rows;
  }

  function grid(a, wacc) {
    var pts = a.sensitivity.grid_points;
    var waccLo = wacc - a.sensitivity.wacc_delta, waccHi = wacc + a.sensitivity.wacc_delta;
    var gLo = a.terminal.terminal_growth - a.sensitivity.terminal_growth_delta;
    var gHi = a.terminal.terminal_growth + a.sensitivity.terminal_growth_delta;
    var cols = [], rowsV = [], cells = [];
    for (var i = 0; i < pts; i++) cols.push(waccLo + (waccHi - waccLo) * i / (pts - 1));
    for (var j = 0; j < pts; j++) rowsV.push(gLo + (gHi - gLo) * j / (pts - 1));
    rowsV.forEach(function (gv) {
      cells.push(cols.map(function (wv) {
        return value(a, wacc, { terminal_growth: gv, wacc: wv }).per_share;
      }));
    });
    return { row_name: '永续增长率 g', col_name: 'WACC', row_field: 'terminal_growth',
      col_field: 'wacc', row_values: rowsV, col_values: cols, cells: cells, price: a.market.price };
  }

  function solve(a, wacc, field) {
    var lo, hi, apply;
    var n = a.drivers.revenue_growth.length;
    if (field === 'wacc') { lo = 0.04; hi = 0.20; apply = function (v) { return { wacc: v }; }; }
    else if (field === 'terminal_growth') { lo = -0.01; hi = wacc - 0.005; apply = function (v) { return { terminal_growth: v }; }; }
    else if (field === 'revenue_growth') { lo = 0; hi = 0.6; apply = function (v) { return { revenue_growth: broadcast(v, n) }; }; }
    else if (field === 'ebit_margin') { lo = 0.05; hi = 0.6; apply = function (v) { return { ebit_margin: broadcast(v, n) }; }; }
    else { lo = 0; hi = 0.6; apply = function (v) { return { tax_rate: v }; }; }
    var target = a.market.price, f = function (v) { return value(a, wacc, apply(v)).per_share - target; };
    var fLo = f(lo), fHi = f(hi), mid = lo;
    if (fLo * fHi > 0) return null;
    for (var k = 0; k < 80; k++) {
      mid = (lo + hi) / 2;
      var fm = f(mid);
      if (Math.abs(fm) < 1e-9) break;
      if (fLo * fm <= 0) { hi = mid; fHi = fm; } else { lo = mid; fLo = fm; }
    }
    return mid;
  }

  /* ── 工具 ──────────────────────────────────────────────────────────── */
  function hex(seed, len) {
    var s = '', x = 0;
    for (var i = 0; i < seed.length; i++) x = (x * 131 + seed.charCodeAt(i)) >>> 0;
    while (s.length < (len || 64)) { x = (x * 1103515245 + 12345) >>> 0; s += ('00000000' + x.toString(16)).slice(-8); }
    return s.slice(0, len || 64);
  }

  /* ── 组装演示数据集（结构与真实产物文件一一对应） ─────────────────── */
  function dataset() {
    var a = ASSUMPTIONS;
    var wp = a.wacc_parts;
    var wacc = wp.equity_weight * (wp.risk_free + wp.beta * wp.equity_risk_premium)
      + wp.debt_weight * wp.cost_of_debt * (1 - wp.tax_rate);
    var val = value(a, wacc);
    var tor = torn(a, wacc);
    var g = grid(a, wacc);

    /* 三表预测（在估值内核的利润表基础上补齐资产负债与现金流口径） */
    var p = val.projection, records = [];
    var cash = a.opening_balance.cash;
    p.years.forEach(function (y, i) {
      var revenue = p.revenue[i], ebit = p.ebit[i], da = p.da[i];
      var interest = a.opening_balance.interest_bearing_debt * 0.0385 * 0.92;
      var pretax = ebit - interest;
      var tax = Math.max(0, pretax * a.drivers.tax_rate);
      var net = pretax - tax;
      var ocf = net + da - p.delta_nwc[i];
      var icf = -p.capex[i];
      var dividend = net * 0.30;
      var fcf = ocf + icf - dividend;
      cash = cash + fcf;
      records.push({
        year: y,
        income: {
          revenue: revenue, cost_of_sales: revenue * (1 - a.drivers.gross_margin),
          gross_profit: revenue * a.drivers.gross_margin,
          operating_expenses: revenue * a.drivers.opex_pct_revenue,
          da: da, ebitda: ebit + da, ebit: ebit, interest_expense: interest,
          pretax_profit: pretax, income_tax: tax, net_profit: net,
          net_profit_attributable: net * 0.995
        },
        balance: {
          cash: cash, accounts_receivable: revenue * 0.197, inventory: revenue * 0.231,
          fixed_assets: 682.6 * E8 + p.capex[i] - da,
          other_non_current_assets: a.opening_balance.other_non_current_assets,
          accounts_payable: revenue * 0.214, interest_bearing_debt: 320.0 * E8,
          other_non_current_liabilities: a.opening_balance.other_non_current_liabilities,
          share_capital: a.opening_balance.share_capital,
          retained_earnings: a.opening_balance.retained_earnings + net - dividend,
          minority_equity: a.opening_balance.minority_equity
        },
        cashflow: {
          operating: ocf, investing: icf, financing: -dividend, net_change: fcf, cash_end: cash
        }
      });
    });

    /* 勾稽校验：11 项检查 × 5 年 */
    var CHECK_DEFS = [
      ['DQ-01', '数据完整性', '关键科目是否存在零值或缺项'],
      ['BS-01', '资产负债配平', '资产总计 = 负债合计 + 所有者权益合计'],
      ['BS-02', '权益构成', '所有者权益 = 归母权益 + 少数股东权益'],
      ['BS-03', '未分配利润滚动', '期末留存 = 期初 + 归母净利润 − 提取盈余公积 − 宣告股利'],
      ['BS-04', '明细不超总额', '明细科目不应超过资产总额'],
      ['CF-01', '现金滚动', '期末现金 = 期初 + 经营 + 投资 + 筹资'],
      ['CF-02', '现金口径', '现金流量表期末现金 = 资产负债表货币资金'],
      ['X-01', '收入与应收背离', '收入增速与应收账款增速的偏离度'],
      ['X-02', '利润现金转化', '经营现金流 / 净利润'],
      ['X-03', '非经常性损益占比', '非经常性损益占净利润比例'],
      ['X-04', '量级合理性', '关键科目量级是否合理']
    ];
    var reports = records.map(function (r, i) {
      var checks = CHECK_DEFS.map(function (cd, k) {
        var status = 'pass', diff = 0, metric = null, note = '';
        if (cd[0] === 'X-01' && i >= 2) { status = 'warning'; metric = 0.081 + i * 0.004; note = '应收账款增速高于收入增速，需关注回款周期'; }
        if (cd[0] === 'X-02') { metric = 1.12 + i * 0.03; }
        if (cd[0] === 'X-03') { status = i === 0 ? 'skipped' : 'pass'; metric = i === 0 ? null : 0.031; note = i === 0 ? '输入缺少非经常性损益明细，无法判断（不等于通过）' : ''; }
        if (cd[0] === 'BS-01') { diff = 0; }
        return { check_id: cd[0], category: cd[1], description: cd[2], status: status,
          expected: null, actual: null, difference: diff, metric: metric,
          tolerance: cd[0] === 'X-01' ? 0.05 : (cd[0] === 'X-02' ? 1.0 : null), note: note };
      });
      var summary = { pass: 0, warning: 0, fail: 0, skipped: 0, total: checks.length };
      checks.forEach(function (c) { summary[c.status]++; });
      return { fiscal_year: r.year, source: '演示样本', summary: summary, checks: checks };
    });

    /* 可比公司：演示样本按「给定倍数 → 反推基本面」的方式构造，
       这样它们的倍数区间落在合理范围，与目标公司的可比性也站得住。
       注意：目标公司的企业价值用**市场口径**（市值 + 净债务），
       不是 DCF 算出来的企业价值 —— 相对估值比较的是市场定价。 */
    function peerFrom(code, name, price, sharesYi, pe, pb, ps, evEbitda, evEbit) {
      var shares = sharesYi * E8;
      var mcap = price * shares;
      var ev = mcap;                       // 演示样本按净债务为 0 处理
      return {
        code: code, name: name, price: price, diluted_shares: shares,
        market_cap: mcap, net_debt: 0, enterprise_value: ev,
        net_profit_attributable: mcap / pe,
        book_equity_attributable: mcap / pb,
        revenue: mcap / ps,
        ebitda: ev / evEbitda,
        ebit: ev / evEbit,
        interest_bearing_debt: 0, cash: 0
      };
    }
    var firstYear = records[0].income;
    var targetNetDebt = a.market.interest_bearing_debt - a.market.cash;
    var targetMcap = a.market.price * a.market.diluted_shares;
    var target = {
      code: a.company.code, name: a.company.name,
      price: a.market.price, diluted_shares: a.market.diluted_shares,
      market_cap: targetMcap, net_debt: targetNetDebt,
      enterprise_value: targetMcap + targetNetDebt,
      net_profit_attributable: firstYear.net_profit,
      book_equity_attributable: a.opening_balance.share_capital + a.opening_balance.retained_earnings,
      revenue: firstYear.revenue, ebitda: firstYear.ebitda, ebit: firstYear.ebit,
      interest_bearing_debt: a.market.interest_bearing_debt, cash: a.market.cash
    };
    var peers = [
      peerFrom('600xxx.SH', '同业甲公司', 42.8, 18.6, 19.5, 3.10, 4.20, 11.5, 14.2),
      peerFrom('300xxx.SZ', '同业乙公司', 31.5, 22.4, 22.4, 2.70, 3.60, 12.6, 15.4),
      peerFrom('002xxx.SZ', '同业丙公司', 66.2, 12.9, 17.8, 3.40, 4.60, 10.9, 13.2),
      peerFrom('688xxx.SH', '同业丁公司', 118.6, 8.4, 26.5, 4.60, 5.00, 14.8, 18.2)
    ];
    function mults(c) {
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
    var names = ['pe', 'pb', 'ps', 'ev_ebitda', 'ev_ebit'];
    function median(arr) {
      if (!arr.length) return null;
      var s = arr.slice().sort(function (x, y) { return x - y; });
      var m = Math.floor(s.length / 2);
      return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
    }
    var stats = {};
    names.forEach(function (n) {
      var vals = [], ex = [];
      peers.concat([target]).forEach(function (c) {
        var v = mults(c)[n];
        if (v == null) ex.push([c.code, '分母非正或数据缺失']); else vals.push(v);
      });
      vals.sort(function (x, y) { return x - y; });
      stats[n] = {
        name: n, values: vals, codes: [], excluded: ex,
        count: vals.length, q1: vals[Math.floor(vals.length * 0.25)] || vals[0],
        median: median(vals), q3: vals[Math.floor(vals.length * 0.75)] || vals[vals.length - 1],
        mean: vals.reduce(function (s, x) { return s + x; }, 0) / vals.length
      };
    });
    var tm = mults(target);
    var implied = names.map(function (n) {
      var v = stats[n].median;
      var per = null, note = '';
      if (v == null) { note = '该倍数样本不足，无法推算'; }
      else if (n === 'pe') per = v * (tm.pe ? target.net_profit_attributable / a.market.diluted_shares : 0);
      else if (n === 'pb') per = v * (target.book_equity_attributable / a.market.diluted_shares);
      else if (n === 'ps') per = v * (target.revenue / a.market.diluted_shares);
      else {
        var impliedEv = v * (n === 'ev_ebitda' ? target.ebitda : target.ebit);
        per = (impliedEv - target.net_debt) / a.market.diluted_shares;
      }
      return { multiple: n, stat_name: 'median', peer_value: v, implied_per_share: per, note: note };
    }).filter(function (x) { return x.implied_per_share != null; });

    /* 足球场区间：DCF 用敏感性上下界，其余用倍数四分位推算 */
    var torLo = Math.min.apply(null, tor.map(function (r) { return Math.min(r.per_share_at_low, r.per_share_at_high); }));
    var torHi = Math.max.apply(null, tor.map(function (r) { return Math.max(r.per_share_at_low, r.per_share_at_high); }));
    var LABEL = { pe: 'PE', pb: 'PB', ps: 'PS', ev_ebitda: 'EV/EBITDA', ev_ebit: 'EV/EBIT' };
    var bands = [{ name: 'DCF（现金流折现）', low: torLo, central: val.per_share, high: torHi }];
    implied.forEach(function (x) {
      var s = stats[x.multiple];
      var lo = null, hi = null;
      if (s.q1 && s.median && s.q3) {
        var ratio = x.implied_per_share / s.median;
        lo = s.q1 * ratio; hi = s.q3 * ratio;
      }
      if (lo && hi) bands.push({ name: LABEL[x.multiple] + ' 中位数区间', low: lo, central: x.implied_per_share, high: hi });
    });
    var ff = {
      price: a.market.price,
      overall_low: Math.min.apply(null, bands.map(function (b) { return b.low; })),
      overall_high: Math.max.apply(null, bands.map(function (b) { return b.high; })),
      bands: bands,
      warnings: ['DCF 区间与相对估值区间部分重叠，两条估值线互相支持']
    };

    /* 反向估值 */
    var REV_DEFS = [['WACC', 'wacc'], ['永续增长率 g', 'terminal_growth'],
      ['收入增速', 'revenue_growth'], ['EBIT 利润率', 'ebit_margin'], ['所得税率', 'tax_rate']];
    var reverse = REV_DEFS.map(function (rd) {
      var solved = solve(a, wacc, rd[1]);
      var baseVal2 = rd[1] === 'wacc' ? wacc
        : rd[1] === 'terminal_growth' ? a.terminal.terminal_growth
        : rd[1] === 'revenue_growth' ? avg(a.drivers.revenue_growth)
        : rd[1] === 'ebit_margin' ? avg(a.drivers.ebit_margin) : a.drivers.tax_rate;
      return {
        name: rd[0], field: rd[1], base_value: baseVal2,
        implied_value: solved, target_per_share: a.market.price,
        achieved_per_share: a.market.price,
        direction: solved == null ? 'same' : (solved > baseVal2 ? 'higher' : 'lower'),
        residual: 0, skipped: solved == null
      };
    });

    /* 执行轨迹（与 trace.jsonl 字段一致） */
    var STEP_DEFS = [
      ['load_config', 'pipeline.config', 0.21],
      ['forecast', 'algorithm.finai.forecast', 0.34],
      ['validate', 'algorithm.finai.validate', 0.28],
      ['wacc', 'algorithm.finai.wacc', 0.05],
      ['valuation', 'algorithm.finai.dcf', 0.18],
      ['sensitivity', 'algorithm.finai.sensitivity', 1.46],
      ['reverse', 'algorithm.finai.reverse_dcf', 2.17],
      ['comps', 'algorithm.finai.comps', 0.46],
      ['football_field', 'algorithm.finai.comps', 0.92],
      ['narrative', 'algorithm.output.narrative', 31.15],
      ['write_outputs', 'algorithm.output.export_run', 4.30]
    ];
    var t0 = Date.parse('2026-10-03T14:07:10+08:00');
    var acc = 0;
    var steps = STEP_DEFS.map(function (s, i) {
      acc += s[2];
      return { seq: i + 1, step: s[0], tool: s[1], status: 'ok', duration_s: s[2],
        timestamp: new Date(t0 + acc * 1000).toISOString(),
        model: s[0] === 'narrative' ? 'deepseek-v4-pro' : null,
        prompt_version: s[0] === 'narrative' ? 'v3' : null };
    });

    /* 产物清单（编号与真实产物一致） */
    var ART = [
      ['01_projections.json', '数据', 48213], ['02_validation_report.json', '数据', 21874],
      ['03_valuation.json', '数据', 9642], ['04_tornado.csv', '数据', 1284],
      ['05_sensitivity.csv', '数据', 928], ['06_reverse_dcf.json', '数据', 4310],
      ['07_comps.csv', '数据', 2260], ['08_comps_implied.csv', '数据', 1436],
      ['09_football_field.json', '数据', 1873], ['10_run_manifest.json', '数据', 7120],
      ['charts/11_football_field.png', '图表', 106910], ['charts/12_tornado.png', '图表', 118074],
      ['charts/13_sensitivity_heatmap.png', '图表', 124343], ['charts/14_peer_multiples.png', '图表', 114476],
      ['charts/15_forecast_trend.png', '图表', 125383], ['charts/16_validation_summary.png', '图表', 114096],
      ['17_report.md', '报告', 70184], ['18_report.html', '报告', 48646], ['19_report.pdf', '报告', 862410]
    ];
    var artifacts = ART.map(function (x) {
      return { file: x[0], kind: x[1], bytes: x[2], sha256: hex('artifact:' + x[0], 64) };
    });

    var inputs = [
      { path: 'data/assumptions.toml', sha256: hex('in:assumptions', 64), bytes: 14283 },
      { path: 'data/market.csv', sha256: hex('in:market', 64), bytes: 1330 },
      { path: 'data/peers.csv', sha256: hex('in:peers', 64), bytes: 2993 }
    ];

    var manifest = {
      run_id: 'DEMO-20261003T140710',
      created_at: '2026-10-03T14:07:41',
      python: '3.11.9',
      platform: 'Windows-11-10.0.26100',
      inputs: {
        code: { commit: hex('commit', 40), commit_short: hex('commit', 7), branch: 'main', dirty: false },
        inputs: inputs.reduce(function (o, x) { o[x.path] = x.sha256; return o; }, {}),
        steps: steps.map(function (s) { return { seq: s.seq, step: s.step, status: s.status, duration_s: s.duration_s }; }),
        run_parameters: {
          company: a.company,
          assumptions: a.drivers,
          opening_balance: a.opening_balance,
          wacc: { value: wacc, parts: wp },
          sensitivity: a.sensitivity,
          evidence: a.evidence,
          notes: { peers: ['同业丁公司规模显著小于目标公司，相对估值时权重较低'], reverse_skipped: [] }
        },
        narrative: {
          ok: true, model: 'deepseek-v4-pro', endpoint: 'https://api.deepseek.com',
          prompt_version: 'v3', rounds: 2, temperature: 0.3, elapsed_s: 31.15,
          usage: { prompt_tokens: 18240, completion_tokens: 6318, total_tokens: 24558 },
          prompt_sha256: hex('prompt', 64), response_sha256: hex('resp', 64),
          /* 与 algorithm/output/narrative.py 的 NARRATIVE_SECTIONS 保持一致 */
          sections: ['final_verdict', 'investment_conclusion', 'executive_summary',
            'company_overview', 'assumption_review', 'financial_validation',
            'valuation_analysis', 'sensitivity_analysis', 'reverse_dcf', 'risks'],
          notes: [], number_warnings: []
        }
      },
      outputs: artifacts,
      warnings: []
    };

    return {
      demo: true,
      run_id: manifest.run_id,
      company: a.company,
      as_of: a.market.as_of,
      created_at: '2026-10-03 14:07',
      assumptions: a.drivers,
      opening_balance: a.opening_balance,
      terminal: a.terminal,
      wacc: wacc,
      wacc_parts: wp,
      market: a.market,
      evidence_ledger: a.evidence,
      valuation: val,
      projections: { source: '内置演示样本', years: records, worst_balance_gap: 0 },
      validation: { count: reports.length, reports: reports },
      tornado: tor,
      grid: g,
      reverse: { results: reverse },
      comps: { target: target, peers: peers, stats: stats, implied: implied },
      football_field: ff,
      steps: steps,
      artifacts: artifacts,
      inputs: inputs,
      manifest: manifest,
      report_md: reportMarkdown(a, val, tor, g, reports, reverse)
    };
  }

  /* 演示报告：结构与真实 17_report.md 的十个章节一致 */
  function reportMarkdown(a, v, tor, g, reports, reverse) {
    var money = function (x) { return (x / E8).toFixed(2); };
    var pct = function (x) { return (x * 100).toFixed(2) + '%'; };
    var warn = reports.reduce(function (s, r) { return s + r.summary.warning; }, 0);
    var skip = reports.reduce(function (s, r) { return s + r.summary.skipped; }, 0);
    return [
      '# ' + a.company.name + '（' + a.company.code + '）估值报告',
      '',
      '> 运行编号 DEMO-20261003T140710 ｜ 数据基准日 ' + a.market.as_of + ' ｜ 本报告由内置演示样本生成，仅用于界面演示',
      '',
      '## 1. 投资结论 【推论】【观点】',
      '',
      '> **AI 生成 · 结论**',
      '',
      '基于现金流折现模型，目标公司每股内在价值约 **' + v.per_share.toFixed(2) + ' 元**，相对现价 '
        + a.market.price.toFixed(2) + ' 元隐含 **' + pct(v.implied_return) + '** 的空间。'
        + '终值占企业价值的 ' + pct(v.terminal_share_of_ev) + '，说明估值结果对永续假设较为敏感；'
        + '反向求解显示，现价隐含的永续增长率约为 ' + pct(reverse[1].implied_value) + '，低于我们采用的 '
        + pct(a.terminal.terminal_growth) + '。',
      '',
      '## 2. 估值摘要 【事实】【推论】',
      '',
      '| 指标 | 数值 |',
      '| --- | --- |',
      '| 企业价值（亿元） | ' + money(v.enterprise_value) + ' |',
      '| 权益价值（亿元） | ' + money(v.equity_value) + ' |',
      '| 净债务（亿元） | ' + money(v.net_debt) + ' |',
      '| 每股价值（元） | ' + v.per_share.toFixed(2) + ' |',
      '| 当前股价（元） | ' + a.market.price.toFixed(2) + ' |',
      '| 隐含回报 | ' + pct(v.implied_return) + ' |',
      '| 终值占企业价值 | ' + pct(v.terminal_share_of_ev) + ' |',
      '| WACC | ' + pct(v.wacc) + ' |',
      '',
      '## 3. 公司与预测概览 【事实】',
      '',
      '预测期为 ' + v.projection.years[0] + ' 至 ' + v.projection.years[v.projection.years.length - 1]
        + '，共 ' + v.projection.years.length + ' 年。基期营业收入 ' + money(a.drivers.revenue_base) + ' 亿元。',
      '',
      '| 年份 | 营业收入（亿元） | EBIT（亿元） | NOPAT（亿元） | FCFF（亿元） | 现值（亿元） |',
      '| --- | --- | --- | --- | --- | --- |',
      v.projection.years.map(function (y, i) {
        return '| ' + y + ' | ' + money(v.projection.revenue[i]) + ' | ' + money(v.projection.ebit[i])
          + ' | ' + money(v.projection.nopat[i]) + ' | ' + money(v.projection.fcff[i])
          + ' | ' + money(v.pv_fcff[i]) + ' |';
      }).join('\n'),
      '',
      '## 4. 假设合理性审查 【推论】',
      '',
      '### 4.1 假设依据台账',
      '',
      '以下依据由数据文件登记，未经大模型加工。',
      '',
      '| 参数 | 依据 |',
      '| --- | --- |',
      Object.keys(a.evidence).map(function (k) { return '| `' + k + '` | ' + a.evidence[k] + ' |'; }).join('\n'),
      '',
      '### 4.2 审查意见（大模型推理）',
      '',
      '> **AI 生成 · 推论**',
      '',
      '多数参数仍为待查状态，其中 beta、股权风险溢价与债务成本三条对 WACC 的影响最大，'
        + '建议在提交前替换为有明确来源的数值。',
      '',
      '## 5. 财务勾稽校验 【事实】【推论】',
      '',
      '预测期共 ' + reports.length + ' 个年度、' + (reports[0].checks.length * reports.length)
        + ' 项检查，其中告警 ' + warn + ' 项、跳过 ' + skip + ' 项，无失败项。'
        + '跳过项表示输入缺失、无法判断，**不等同于通过**。',
      '',
      '## 6. 估值分析 【事实】【推论】',
      '',
      '### 6.1 现金流折现',
      '',
      '终值采用永续增长法，永续增长率 ' + pct(a.terminal.terminal_growth) + '，隐含退出倍数 '
        + v.implied_exit_multiple.toFixed(2) + ' 倍。',
      '',
      '### 6.2 相对估值',
      '',
      '可比公司中位数倍数与隐含每股价值见「相对估值」页。',
      '',
      '### 6.3 各方法的估值区间',
      '',
      '各方法区间见「相对估值」页的估值区间视图。',
      '',
      '## 7. 敏感性与区间归因 【事实】【推论】',
      '',
      '单因素敏感性排序中，影响最大的是 **' + tor[0].name + '**，上下界之间的估值波动幅度为 '
        + (tor[0].swing).toFixed(2) + ' 元/股。',
      '',
      '## 8. 反向估值 【事实】【推论】',
      '',
      '以现价 ' + a.market.price.toFixed(2) + ' 元为目标反解，结果如下：',
      '',
      '| 变量 | 我们的取值 | 市场隐含 | 方向 |',
      '| --- | --- | --- | --- |',
      reverse.filter(function (r) { return !r.skipped; }).map(function (r) {
        return '| ' + r.name + ' | ' + pct(r.base_value) + ' | ' + pct(r.implied_value) + ' | '
          + (r.direction === 'higher' ? '市场更高' : '市场更低') + ' |';
      }).join('\n'),
      '',
      '## 9. 风险与适用边界 【推论】【观点】',
      '',
      '> **AI 生成 · 观点**',
      '',
      '- 终值占比偏高，估值对永续增长率与 WACC 高度敏感，两者各变动 1 个百分点即可显著改变结论。',
      '- 可比公司数量有限，中位数抗偏能力一般，未启用自动异常值剔除。',
      '- 输入假设多为待查状态，结论的稳健性依赖后续数据核实。',
      '',
      '## 10. 附录',
      '',
      '### 10.1 数据文件索引',
      '',
      '01–10 号数据文件见产物清单。',
      '',
      '### 10.2 图表索引',
      '',
      '11–16 号图表见「总览」页图表区。',
      '',
      '### 10.3 大模型使用与数字校验',
      '',
      '模型 `deepseek-v4-pro`，提示词版本 `v3`，温度 0.3，token 用量 24,558。'
        + '正文中出现的每一个数字均与事实表比对，未发现事实表之外的数字。',
      '',
      '### 10.4 过程追溯',
      '',
      '完整执行轨迹见「复现凭证」页。',
      ''
    ].join('\n');
  }

  global.Demo = {
    dataset: dataset,
    assumptions: ASSUMPTIONS,
    valueAt: function (overrides) {
      var a = ASSUMPTIONS, wp = a.wacc_parts;
      var wacc = wp.equity_weight * (wp.risk_free + wp.beta * wp.equity_risk_premium)
        + wp.debt_weight * wp.cost_of_debt * (1 - wp.tax_rate);
      return value(a, wacc, overrides);
    }
  };
})(window);
