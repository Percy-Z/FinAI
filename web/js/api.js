/* 数据层
   ----------------------------------------------------------------------
   原则（PRD 第 3.2 节）：
     1. 前端只读 runs/、logs/、data/ 三个目录，唯一写操作是触发一次运行；
     2. 所有金额、比率、区间必须来自产物文件；前端只做展示层派生
        （格式化、排序、差值、颜色、缩写）；
     3. 产物缺失时显示「本次未产出」，不显示 0。
   本地服务不可用或没有运行记录时，回落到内置演示数据集（Demo.dataset()），
   并在界面上明确标注「演示数据」。
   ---------------------------------------------------------------------- */
(function (global) {
  'use strict';

  var FILES = {
    projections: '01_projections.json',
    validation: '02_validation_report.json',
    valuation: '03_valuation.json',
    tornado: '04_tornado.csv',
    grid: '05_sensitivity.csv',
    reverse: '06_reverse_dcf.json',
    comps: '07_comps.csv',
    compsImplied: '08_comps_implied.csv',
    football: '09_football_field.json',
    manifest: '10_run_manifest.json',
    report: '17_report.md'
  };

  /* ── 基础工具 ─────────────────────────────────────────────────────── */
  function toNum(x) {
    if (x === null || x === undefined) return null;
    var s = String(x).replace(/,/g, '').replace(/%$/, '').trim();
    if (s === '' || s === '-' || s === '—') return null;
    var v = Number(s);
    return isFinite(v) ? v : null;
  }

  /* CSV：支持 BOM、引号、CRLF */
  function parseCSV(text) {
    var rows = [], row = [], cur = '', q = false;
    text = String(text).replace(/^\uFEFF/, '');
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) {
        if (ch === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; }
        else cur += ch;
      } else if (ch === '"') q = true;
      else if (ch === ',') { row.push(cur); cur = ''; }
      else if (ch === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
      else if (ch !== '\r') cur += ch;
    }
    if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
    return rows.filter(function (r) { return r.length && !(r.length === 1 && r[0].trim() === ''); });
  }

  function parseJSONL(text) {
    return String(text).split(/\r?\n/).filter(function (l) { return l.trim(); })
      .map(function (l) { try { return JSON.parse(l); } catch (e) { return null; } })
      .filter(Boolean);
  }

  /* 极简 TOML 读取：只取 [section] 下的 key = value，够读 data/assumptions.toml。
     注释、字符串、布尔、数字数组都能处理；复杂 TOML 不在此列（也不需要）。 */
  function parseToml(text) {
    var out = {}, cur = null;
    String(text).split(/\r?\n/).forEach(function (line) {
      var l = line.replace(/(^|\s)#.*$/, '').trim();
      if (!l) return;
      var sec = l.match(/^\[([^\]]+)\]$/);
      if (sec) { cur = sec[1].trim(); out[cur] = out[cur] || {}; return; }
      if (!cur) return;
      var kv = l.match(/^("[^"]+"|[^=]+?)\s*=\s*(.+)$/);
      if (!kv) return;
      var k = kv[1].trim().replace(/^"|"$/g, '');
      var v = kv[2].trim().replace(/\s+#.*$/, '');
      if (/^".*"$/.test(v)) v = v.slice(1, -1);
      else if (/^(true|false)$/.test(v)) v = (v === 'true');
      else if (/^\[.*\]$/.test(v)) {
        v = v.slice(1, -1).split(',').map(function (x) { return Number(x.trim()); })
          .filter(function (x) { return isFinite(x); });
      } else { var n = Number(v); v = isFinite(n) && v !== '' ? n : v; }
      out[cur][k] = v;
    });
    return out;
  }

  function marketRow(text) {
    if (!text) return null;
    var rows = parseCSV(text).filter(function (r) { return r[0] !== '#' && !/^#/.test(String(r[0])); });
    if (!rows.length) return null;
    var head = rows[0];
    var idx = function (k) { return head.indexOf(k); };
    var row = null;
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][idx('role')] === 'target') { row = rows[i]; break; }
    }
    if (!row) row = rows[1];
    if (!row) return null;
    var pick = function (k) { return toNum(row[idx(k)]); };
    return {
      price: pick('price'), diluted_shares: pick('diluted_shares'),
      interest_bearing_debt: pick('interest_bearing_debt'), cash: pick('cash'),
      minority_interest: pick('minority_interest'), preferred_stock: pick('preferred_stock'),
      non_operating_assets: pick('non_operating_assets'),
      as_of: row[idx('as_of')] || ''
    };
  }

  /* ── 网络 ─────────────────────────────────────────────────────────── */
  function get(url, asText) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url);
      return asText ? r.text() : r.json();
    });
  }

  function fileUrl(runId, name) { return '/runs/' + encodeURIComponent(runId) + '/' + name; }

  function status() {
    return fetch('/api/status', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('status ' + r.status); return r.json(); });
  }

  function trigger() {
    return fetch('/api/run', { method: 'POST' })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); });
  }

  /* 取数向导用的几个接口。
     为什么统一返回 {ok, status, body} 而不是抛异常：界面要按错误内容给出
     「下一步怎么做」（比如「这家没上市，用它的上市母公司」），抛异常会把
     那句话淹在一句 HTTP 500 里。 */
  function request(method, url, body) {
    var opts = { method: method, cache: 'no-store' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.json()
        .catch(function () { return {}; })
        .then(function (b) { return { ok: r.ok && b.ok !== false, status: r.status, body: b }; });
    }).catch(function (err) {
      return { ok: false, status: 0, body: { error: String((err && err.message) || err) } };
    });
  }

  function companies() { return request('GET', '/api/companies'); }
  function ingest(company) {
    var url = '/api/ingest' + (company ? '?company=' + encodeURIComponent(company) : '');
    return request('GET', url);
  }
  function resolveName(keyword) { return request('POST', '/api/resolve', { keyword: keyword }); }
  function planDraft(company) {
    return request('GET', '/api/plan/draft?company=' + encodeURIComponent(company));
  }
  function saveDraft(company, fields) {
    return request('POST', '/api/plan/draft', { company: company, fields: fields });
  }
  function startPlan(spec) { return request('POST', '/api/plan', spec); }
  function startIngestRun(company) { return request('POST', '/api/ingest/run', { company: company }); }
  function importReports(payload) { return request('POST', '/api/reports', payload); }

  /* ── 归一化：把产物文件拼成页面需要的一份数据 ─────────────────────── */
  function normalizeComps(compsRows, impliedRows) {
    if (!compsRows || compsRows.length < 2) return null;
    var header = compsRows[0];
    var target = null, peers = [], statsRows = [];
    var inStats = false;
    for (var i = 1; i < compsRows.length; i++) {
      var r = compsRows[i];
      if (!r.length || r[0] === '') continue;
      if (r[0] === '统计口径') { inStats = true; continue; }
      if (inStats) { statsRows.push(r); continue; }
      var obj = {
        kind: r[0], code: r[1], name: r[2],
        price: toNum(r[3]), diluted_shares: toNum(r[4]), market_cap: toNum(r[5]),
        net_debt: toNum(r[6]), enterprise_value: toNum(r[7]),
        pe: toNum(r[8]), pb: toNum(r[9]), ps: toNum(r[10]),
        ev_ebitda: toNum(r[11]), ev_ebit: toNum(r[12])
      };
      if (r[0] === '目标') target = obj; else peers.push(obj);
    }
    var LABEL = { 'PE': 'pe', 'PB': 'pb', 'PS': 'ps', 'EV/EBITDA': 'ev_ebitda', 'EV/EBIT': 'ev_ebit' };
    var stats = {};
    statsRows.forEach(function (r) {
      var key = LABEL[r[0]] || r[0];
      stats[key] = {
        name: key, label: r[0], count: toNum(r[1]), q1: toNum(r[2]),
        median: toNum(r[3]), q3: toNum(r[4]), mean: toNum(r[5]), excluded: []
      };
    });
    var implied = [];
    (impliedRows || []).slice(1).forEach(function (r) {
      if (!r.length || !r[0]) return;
      implied.push({
        multiple: LABEL[r[0]] || r[0], label: r[0], stat_name: r[1],
        peer_value: toNum(r[2]), implied_per_share: toNum(r[3]),
        relative_text: r[4] || '', note: r[5] || ''
      });
    });
    return { header: header, target: target, peers: peers, stats: stats, implied: implied };
  }

  function normalizeGrid(rows) {
    if (!rows || rows.length < 2) return null;
    var head = rows[0];
    var first = String(head[0] || '');
    var colName = 'WACC', rowName = '永续增长率 g';
    if (first.indexOf('\\') > -1) {
      var parts = first.split('\\');
      rowName = parts[0].trim(); colName = parts[1].trim();
    }
    return {
      row_name: rowName, col_name: colName,
      col_values: head.slice(1).map(toNum).filter(function (x) { return x !== null; }),
      row_values: [], cells: []
    };
  }

  /* 读一次运行的全部产物。任何一项失败都不影响其它项。 */
  function loadRun(runId) {
    var req = {
      projections: get(fileUrl(runId, FILES.projections), true).then(JSON.parse),
      validation: get(fileUrl(runId, FILES.validation), true).then(JSON.parse),
      valuation: get(fileUrl(runId, FILES.valuation), true).then(JSON.parse),
      tornado: get(fileUrl(runId, FILES.tornado), true).then(parseCSV),
      grid: get(fileUrl(runId, FILES.grid), true).then(parseCSV),
      reverse: get(fileUrl(runId, FILES.reverse), true).then(JSON.parse),
      comps: get(fileUrl(runId, FILES.comps), true).then(parseCSV),
      compsImplied: get(fileUrl(runId, FILES.compsImplied), true).then(parseCSV),
      football: get(fileUrl(runId, FILES.football), true).then(JSON.parse),
      manifest: get(fileUrl(runId, FILES.manifest), true).then(JSON.parse),
      report: get(fileUrl(runId, FILES.report), true).catch(function () { return null; }),
      trace: get('/logs/' + encodeURIComponent(runId) + '/trace.jsonl', true)
        .then(parseJSONL).catch(function () { return []; }),
      logManifest: get('/logs/' + encodeURIComponent(runId) + '/run_manifest.json', true)
        .then(JSON.parse).catch(function () { return null; }),
      config: get('/data/assumptions.toml', true).then(parseToml).catch(function () { return null; }),
      marketCsv: get('/data/market.csv', true).catch(function () { return null; })
    };
    var keys = Object.keys(req);
    return Promise.all(keys.map(function (k) {
      return req[k].catch(function () { return null; });
    })).then(function (vals) {
      var o = {};
      keys.forEach(function (k, i) { o[k] = vals[i]; });
      if (!o.valuation) return null;   // 连估值结果都没有，视作这次运行不可用
      return shape(runId, o);
    });
  }

  var MULT_LABEL = { pe: 'PE', pb: 'PB', ps: 'PS', ev_ebitda: 'EV/EBITDA', ev_ebit: 'EV/EBIT' };

  function shape(runId, o) {
    var params = (o.manifest && o.manifest.inputs && o.manifest.inputs.run_parameters) || {};
    var cfg = o.config || {};
    var company = params.company || cfg.company || { name: '未命名标的', code: '', source: '' };
    var market = marketRow(o.marketCsv) || null;
    var tor = (o.tornado || []).slice(1).map(function (r) {
      return {
        name: r[0], field: r[1], low: toNum(r[2]), base: toNum(r[3]), high: toNum(r[4]),
        per_share_at_low: toNum(r[5]), per_share_at_high: toNum(r[6]),
        swing: toNum(r[7]), per_share_base: toNum(r[8])
      };
    }).filter(function (r) { return r.name; });
    tor.sort(function (a, b) { return (b.swing || 0) - (a.swing || 0); });

    var grid = normalizeGrid(o.grid);
    if (grid && o.grid.length > 1) {
      for (var i = 1; i < o.grid.length; i++) {
        var row = o.grid[i];
        grid.row_values.push(toNum(row[0]));
        grid.cells.push(row.slice(1).map(toNum));
      }
    }

    var inputs = [];
    var ip = (o.manifest && o.manifest.inputs && o.manifest.inputs.inputs) || {};
    Object.keys(ip).forEach(function (k) { inputs.push({ path: k, sha256: ip[k] }); });

    return {
      demo: false,
      run_id: runId,
      company: company,
      as_of: (market && market.as_of) || '',
      created_at: (o.manifest && o.manifest.created_at) || '',
      assumptions: params.assumptions || null,
      opening_balance: params.opening_balance || null,
      terminal: cfg.terminal || params.terminal || null,
      wacc: (params.wacc && params.wacc.value) || (o.valuation && o.valuation.wacc) || null,
      wacc_parts: (params.wacc && (params.wacc.parts || params.wacc)) || null,
      sensitivity: params.sensitivity || null,
      market: market || params.market || null,
      evidence_ledger: params.evidence || {},
      notes: params.notes || {},
      valuation: o.valuation,
      projections: o.projections,
      validation: o.validation,
      tornado: tor,
      grid: grid,
      reverse: o.reverse,
      comps: normalizeComps(o.comps, o.compsImplied),
      football_field: o.football,
      steps: (o.trace || []).map(function (s) {
        return { seq: s.seq, step: s.step, tool: s.tool, status: s.status,
          duration_s: s.duration_s, timestamp: s.timestamp, model: s.model,
          prompt_version: s.prompt_version, extra: s.extra };
      }),
      artifacts: (o.manifest && o.manifest.outputs) || [],
      inputs: inputs,
      manifest: o.manifest,
      logManifest: o.logManifest,
      report_md: o.report,
      warnings: ((o.manifest && o.manifest.warnings) || []).concat(
        (o.logManifest && o.logManifest.warnings) || [])
    };
  }

  /* ── 对外：取当前要展示的数据 ─────────────────────────────────────── */
  var cache = {};

  function load(runId, attempt) {
    attempt = attempt || 0;
    if (!runId) return Promise.resolve(withDemoFlag(Demo.dataset(), 'no-run'));
    if (cache[runId]) return Promise.resolve(cache[runId]);
    return loadRun(runId).then(function (data) {
      if (!data) throw new Error('本次运行的产物不完整');
      cache[runId] = data;
      return data;
    }).catch(function () {
      /* 失败先重试一次，再退回演示数据：本地服务偶尔抖一下，
         不该让整个界面直接变成演示数据。 */
      if (attempt < 1) {
        return new Promise(function (resolve) { setTimeout(resolve, 500); })
          .then(function () { return load(runId, attempt + 1); });
      }
      return withDemoFlag(Demo.dataset(), 'load-failed');
    });
  }

  function withDemoFlag(d, reason) {
    d.demo = true;
    d.demo_reason = reason;
    return d;
  }

  function clearCache() { cache = {}; }

  /* ── 隐藏的运行记录 ────────────────────────────────────────────────────
     按 PRD FE-G03：只隐藏、不删除。前端对产物目录是只读的，所以"隐藏"
     只记在浏览器本地（localStorage），不会动 runs/ 下的任何文件。
     ------------------------------------------------------------------ */
  var HIDDEN_KEY = 'finai.hiddenRuns.v1';

  function hiddenList() {
    try {
      var raw = localStorage.getItem(HIDDEN_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter(function (x) { return typeof x === 'string'; }) : [];
    } catch (err) {
      return [];   // 隐私模式 / 存储被禁用时降级为「什么都不隐藏」
    }
  }

  function hiddenSave(arr) {
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(arr)); } catch (err) { /* 忽略 */ }
  }

  var hiddenRuns = {
    list: hiddenList,
    has: function (id) { return hiddenList().indexOf(id) >= 0; },
    add: function (id) {
      var a = hiddenList();
      if (a.indexOf(id) < 0) a.push(id);
      hiddenSave(a);
    },
    remove: function (id) {
      hiddenSave(hiddenList().filter(function (x) { return x !== id; }));
    }
  };

  global.API = {
    FILES: FILES,
    status: status,
    trigger: trigger,
    request: request,
    companies: companies,
    ingest: ingest,
    resolveName: resolveName,
    planDraft: planDraft,
    saveDraft: saveDraft,
    startPlan: startPlan,
    startIngestRun: startIngestRun,
    importReports: importReports,
    get: get,
    load: load,
    loadRun: loadRun,
    fileUrl: fileUrl,
    parseCSV: parseCSV,
    parseJSONL: parseJSONL,
    toNum: toNum,
    clearCache: clearCache,
    hiddenRuns: hiddenRuns,
    multLabel: MULT_LABEL
  };
})(window);
