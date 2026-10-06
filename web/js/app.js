/* 应用外壳：路由、全局状态、数据装载、全局交互。
   路由形如 #/result/<运行编号>，运行编号进 URL，保证深链接与刷新可恢复。 */
(function () {
  'use strict';

  var PAGES = Pages.list;
  var BY_ID = {};
  PAGES.forEach(function (p) { BY_ID[p.id] = p; });
  var DEFAULT_ROUTE = 'dashboard';

  var State = {
    route: DEFAULT_ROUTE, runId: null, data: null, dataRun: undefined,
    status: null, page: null, timer: null, lastRunning: false,
    reconnect: 0, reconnectTimer: null
  };

  var $ = function (s) { return document.querySelector(s); };

  /* 外壳里有几个 icon-btn 在 HTML 里是空的（只写了 aria-label），
     图标统一在这里注入，避免同一套图标散落在 HTML 与 JS 两处。 */
  function paintStaticIcons() {
    var map = {
      navToggle: 'menu', sidebarToggle: 'menu', drawerCopy: 'copy',
      drawerClose: 'close', lightboxClose: 'close'
    };
    Object.keys(map).forEach(function (id) {
      var el = document.getElementById(id);
      if (el && !el.firstChild) el.innerHTML = Icons.svg(map[id]);
    });
  }

  /* ── 哈希路由 ─────────────────────────────────────────────────────── */
  function parseHash() {
    var h = String(location.hash || '').replace(/^#\/?/, '');
    var parts = h.split('/');
    var id = parts[0] || DEFAULT_ROUTE;
    if (!BY_ID[id]) id = DEFAULT_ROUTE;
    return { id: id, run: parts[1] ? decodeURIComponent(parts[1]) : null };
  }
  function go(id, run) {
    var r = run !== undefined ? run : State.runId;
    location.hash = '#/' + id + (r ? '/' + encodeURIComponent(r) : '');
  }

  /* ── 提示 ─────────────────────────────────────────────────────────── */
  function toast(msg, kind) {
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(function () { el.remove(); }, kind === 'err' ? 8000 : 4200);
  }
  function copy(text, message) {
    var done = function () { toast(message || '已复制到剪贴板'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () { fallback(); });
    } else fallback();
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败', 'err'); }
      ta.remove();
    }
  }

  /* ── 表格复制（PRD FE-I05）─────────────────────────────────────────────
     页面渲染完之后扫一遍：卡片里的数据表都在卡片头挂一个「复制」按钮，
     可以导出 Markdown（贴文档、贴 PPT）或 CSV（贴 Excel）。
     报告正文里的表格不挂 —— 报告本身已经有 MD / HTML / PDF 三种导出。
     ------------------------------------------------------------------ */
  function cellText(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function visibleTableIn(card) {
    var found = null;
    Array.prototype.forEach.call(card.querySelectorAll('table.tbl'), function (el) {
      if (found) return;
      if (el.closest('[hidden]') || el.closest('.prose')) return;
      found = el;
    });
    return found;
  }

  function tableRows(table) {
    var head = [];
    Array.prototype.forEach.call(table.querySelectorAll('thead tr'), function (tr) {
      head = Array.prototype.map.call(tr.children, cellText);
    });
    var body = [];
    Array.prototype.forEach.call(table.querySelectorAll('tbody tr'), function (tr) {
      if (tr.hidden || tr.closest('[hidden]')) return;
      body.push(Array.prototype.map.call(tr.children, cellText));
    });
    return { head: head, body: body };
  }

  function tableToMarkdown(table) {
    var r = tableRows(table);
    var esc = function (s) { return String(s).replace(/\|/g, '\\|'); };
    var out = [];
    if (r.head.length) {
      out.push('| ' + r.head.map(esc).join(' | ') + ' |');
      out.push('|' + r.head.map(function () { return ' --- '; }).join('|') + '|');
    }
    r.body.forEach(function (cells) {
      out.push('| ' + cells.map(esc).join(' | ') + ' |');
    });
    return out.join('\n');
  }

  function tableToCSV(table) {
    var r = tableRows(table);
    var rows = (r.head.length ? [r.head] : []).concat(r.body);
    var text = rows.map(function (cells) {
      return cells.map(function (c) {
        c = String(c);
        return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c;
      }).join(',');
    }).join('\r\n');
    /* 前面加 BOM：不然 Excel 打开中文会乱码 */
    return '\ufeff' + text;
  }

  function closeCopyMenu() {
    var m = document.getElementById('copyMenu');
    if (m) m.remove();
  }

  function openCopyMenu(anchor, tableGetter) {
    closeCopyMenu();
    var table = tableGetter();
    if (!table) return;
    var menu = document.createElement('div');
    menu.className = 'mini-menu';
    menu.id = 'copyMenu';
    menu.innerHTML =
      '<button type="button" data-copy-as="md">' + Icons.svg('copy') + '复制为 Markdown</button>' +
      '<button type="button" data-copy-as="csv">' + Icons.svg('table') + '复制为 CSV（Excel）</button>';
    document.body.appendChild(menu);
    var box = anchor.getBoundingClientRect();
    var left = Math.min(
      Math.max(8, box.right - menu.offsetWidth),
      window.innerWidth - menu.offsetWidth - 8
    );
    menu.style.left = left + 'px';
    menu.style.top = Math.min(box.bottom + 6, window.innerHeight - menu.offsetHeight - 8) + 'px';
    menu.addEventListener('click', function (e) {
      var b = e.target.closest('[data-copy-as]');
      if (!b) return;
      var rows = tableRows(table).body.length;
      if (b.getAttribute('data-copy-as') === 'csv') {
        copy(tableToCSV(table), '已复制表格：CSV，共 ' + rows + ' 行（可直接贴进 Excel）');
      } else {
        copy(tableToMarkdown(table), '已复制表格：Markdown，共 ' + rows + ' 行');
      }
      closeCopyMenu();
    });
  }

  function decorateTables(root) {
    Array.prototype.forEach.call(root.querySelectorAll('.card'), function (card) {
      if (!visibleTableIn(card) || card.querySelector('[data-copy-table]')) return;
      var host = card.querySelector('.card-h .right') || card.querySelector('.card-h');
      if (!host) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'icon-btn';
      btn.setAttribute('data-copy-table', '1');
      btn.title = '复制这张表（Markdown / CSV）';
      btn.setAttribute('aria-label', '复制表格');
      btn.innerHTML = Icons.svg('copy');
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        /* 取表放到点击时再解析：勾稽校验页切换年度后可见的表是另一张 */
        openCopyMenu(btn, function () { return visibleTableIn(card); });
      });
      if (host.classList.contains('right')) host.insertBefore(btn, host.firstChild);
      else host.appendChild(btn);
    });
  }

  /* ── 侧边导航 ─────────────────────────────────────────────────────── */
  function renderNav() {
    var groups = [], byGroup = {};
    PAGES.forEach(function (p) {
      if (!byGroup[p.group]) { byGroup[p.group] = []; groups.push(p.group); }
      byGroup[p.group].push(p);
    });
    $('#nav').innerHTML = groups.map(function (g) {
      return '<div class="nav-group"><div class="nav-group-title">' + Charts.esc(g) + '</div>' +
        byGroup[g].map(function (p) {
          return '<button class="nav-item' + (p.id === State.route ? ' active' : '') +
            '" data-nav="' + p.id + '" type="button">' +
            '<span class="nav-ico">' + Icons.svg(p.icon) + '</span><span>' + Charts.esc(p.nav) + '</span></button>';
        }).join('') + '</div>';
    }).join('');
  }

  function renderSideFoot() {
    var d = State.data, s = State.status || {};
    var env = s.environment || {};
    var steps = (d && d.steps) || [];
    var done = steps.filter(function (x) { return x.status === 'ok'; }).length;
    var running = !!s.running;
    $('#sideFoot').innerHTML = '<div class="side-card">' +
      '<div class="row"><span>' + (running ? '正在运行' : '运行状态') + '</span>' +
      '<b>' + (running ? ('执行中 ' + done + '/' + steps.length) : '就绪') + '</b></div>' +
      '<div class="bar' + (running ? '' : '') + '"><i style="width:' +
      (steps.length ? Math.round(done / steps.length * 100) : (d && d.demo ? 100 : 0)) + '%"></i></div>' +
      '<div class="row"><span>图表依赖</span><b>' + (env.matplotlib === false ? '缺失' : '就绪') + '</b></div>' +
      '<div class="row"><span>大模型 Key</span><b>' + (env.api_key === false ? '未配置' : '已配置') + '</b></div>' +
      '</div>';
  }

  /* ── 顶栏 ─────────────────────────────────────────────────────────── */
  function renderTop() {
    var d = State.data, s = State.status || {};
    var v = (d && d.valuation) || {};
    var recent = (s.recent || []).slice();
    var cur = State.runId || (d && d.run_id) || '—';
    if (!recent.some(function (r) { return r.run_id === cur; })) recent.unshift({ run_id: cur, company: d && d.company.name });
    /* 已隐藏的运行不进切换器；当前正在看的那条例外，否则会“看不见自己” */
    recent = recent.filter(function (r) {
      return r.run_id === cur || !API.hiddenRuns.has(r.run_id);
    });

    $('#runPicker').innerHTML =
      '<button type="button" id="rpBtn" aria-haspopup="listbox">' +
      Icons.svg('clock') + '<span class="rp-id mono">' + Charts.esc(cur) + '</span>' + Icons.svg('chevronDown') + '</button>' +
      '<div class="run-menu" id="rpMenu" hidden>' + recent.map(function (r) {
        return '<button type="button" data-run="' + Charts.esc(r.run_id) + '"' +
          (r.run_id === cur ? ' class="on"' : '') + '>' +
          '<span class="rm-top">' + Charts.esc(r.run_id) +
          (r.run_id === cur ? ' <span class="badge pass">当前</span>' : '') + '</span>' +
          '<span class="rm-sub">' + Charts.esc(r.company || '') + '</span></button>';
      }).join('') + '</div>';

    $('#topChips').innerHTML =
      '<span class="chip"><span class="k">WACC</span><b>' + Pages.F.pct(v.wacc, 2) + '</b></span>' +
      '<span class="chip"><span class="k">每股价值</span><b>' + Pages.F.price(v.per_share) + '</b></span>' +
      '<span class="chip"><span class="k">现价</span><b>' + Pages.F.price(v.price) + '</b></span>' +
      '<span class="chip"><span class="k">' + (d && d.demo ? '演示数据' : '运行') + '</span><b>' +
      Charts.esc(d && d.demo ? '演示' : '真实') + '</b></span>';

    var btn = $('#runBtn');
    btn.disabled = !!s.running;
    var narrow = window.innerWidth <= 860;
    btn.innerHTML = Icons.svg(s.running ? 'refresh' : 'play') +
      (s.running ? '运行中…' : (narrow ? '运行' : '运行估值'));
  }

  function renderBanners() {
    var w = (State.data && State.data.warnings) || [];
    $('#banners').innerHTML = w.length
      ? '<div class="banner warn">' + Icons.svg('alert') + '<div><b>运行提示：</b>' +
        w.map(Charts.esc).join('；') + '</div></div>' : '';
  }

  /* ── 页面渲染 ─────────────────────────────────────────────────────── */
  function ensureData() {
    if (State.data && State.dataRun === State.runId) return Promise.resolve(State.data);
    return API.load(State.runId).then(function (d) {
      State.data = d;
      State.dataRun = State.runId;
      if (!State.runId) State.runId = d.run_id;
      return d;
    });
  }

  function renderPage() {
    var ctx = {
      data: State.data, status: State.status, runId: State.runId,
      go: go, setRun: setRun, triggerRun: triggerRun, lightbox: showLightbox,
      toast: toast, copy: copy
    };
    var page = BY_ID[State.route];
    State.page = page;
    $('#pageTitle').textContent = page.title;
    $('#pageSub').textContent = typeof page.sub === 'function' ? page.sub(ctx) : (page.sub || '');
    var view = $('#view');
    view.innerHTML = page.render(ctx);
    if (typeof page.mount === 'function') page.mount(view, ctx);
    decorateTables(view);
    renderNav();
    renderTop();
    renderSideFoot();
    renderBanners();
    view.scrollIntoView({ block: 'start' });
    document.title = page.title + ' · FinAI 自动化估值工作台';
  }

  function paint() {
    return ensureData().then(function () {
      /* 收起上一次页面留下的浮层提示，避免跨页面残留 */
      Array.prototype.forEach.call(document.querySelectorAll('.chart-tip'), function (t) {
        t.hidden = true;
      });
      renderPage();
      /* 万一还是退回了演示数据（服务启动瞬间的竞态），后台自己再试几次，
         成功了就自动切回真实产物 —— 不能让人对着演示数字以为是真的。 */
      var dd = State.data;
      if (dd && dd.demo && dd.demo_reason === 'load-failed' && State.reconnect < 5) {
        State.reconnect += 1;
        clearTimeout(State.reconnectTimer);
        State.reconnectTimer = setTimeout(function () {
          API.clearCache();
          State.dataRun = undefined;
          paint();
        }, 3000);
      }
    }).catch(function (e) {
      $('#view').innerHTML = '<div class="banner fail">' + Icons.svg('alert') +
        '<div><b>页面渲染失败：</b>' + Charts.esc(e && e.message ? e.message : String(e)) + '</div></div>';
      console.error(e);
    });
  }

  function setRun(run) {
    State.runId = run;
    var h = parseHash();
    go(h.id, run);
  }

  /* ── 运行与轮询 ───────────────────────────────────────────────────── */
  function refreshStatus() {
    return API.status().then(function (s) {
      var was = State.lastRunning;
      State.status = s;
      State.lastRunning = !!s.running;
      if (s.running) {
        schedulePoll(1500);
      } else if (was) {
        /* 运行结束：让缓存失效并切到新运行 */
        API.clearCache();
        if (s.last_run) { State.runId = s.last_run; State.dataRun = undefined; }
        toast('估值运行已完成');
        paint();
      } else {
        renderTop(); renderSideFoot();
      }
      return s;
    }).catch(function () {
      /* 服务不可用时保持演示数据，不打断浏览 */
      return null;
    });
  }
  function schedulePoll(ms) {
    clearTimeout(State.timer);
    State.timer = setTimeout(function () { refreshStatus(); }, ms);
  }

  function triggerRun(opts) {
    if (State.status && State.status.running) { toast('已有一次运行在进行中', 'warn'); return; }
    var btn = $('#runBtn');
    btn.disabled = true;
    btn.innerHTML = Icons.svg('refresh') + '提交中…';
    API.trigger().then(function (r) {
      if (!r.ok || (r.body && r.body.ok === false)) {
        toast('触发失败：' + ((r.body && r.body.error) || '未知原因'), 'err');
        btn.disabled = false; renderTop(); return;
      }
      toast('已触发一次估值运行，正在执行…');
      State.lastRunning = false;
      go('run');
      refreshStatus();
    }).catch(function () {
      toast('无法连接到本地服务，请确认 pipeline.web 已启动', 'err');
      btn.disabled = false; renderTop();
    });
  }

  /* ── 证据抽屉 ─────────────────────────────────────────────────────── */
  function openDrawer(title, html) {
    $('#drawerTitle').textContent = title;
    $('#drawerBody').innerHTML = html;
    $('#drawer').hidden = false;
    $('#scrim').hidden = false;
    $('#drawerClose').focus();
  }
  function closeDrawer() {
    $('#drawer').hidden = true;
    $('#scrim').hidden = true;
  }
  function showLightbox(title, url) {
    $('#lightboxTitle').textContent = title;
    $('#lightboxBody').innerHTML = '<img src="' + url + '" alt="' + Charts.esc(title) + '">';
    $('#lightbox').hidden = false;
  }

  /* ── 命令面板 ─────────────────────────────────────────────────────── */
  var PAL = [];
  function buildPalette() {
    PAL = PAGES.map(function (p) {
      return { label: '打开：' + p.nav, kind: '页面', run: function () { go(p.id); } };
    }).concat([
      { label: '运行一次估值流水线', kind: '动作', run: function () { triggerRun({}); } },
      { label: '切换演示模式', kind: '动作', run: togglePresent },
      { label: '打开报告中心', kind: '页面', run: function () { go('report'); } },
      { label: '查看复现凭证', kind: '页面', run: function () { go('evidence'); } },
      { label: '复制当前运行编号', kind: '动作', run: function () { copy(State.runId || ''); } }
    ]);
  }
  function openPalette() {
    buildPalette();
    $('#palette').hidden = false;
    var inp = $('#paletteInput');
    inp.value = ''; renderPalette(''); inp.focus();
  }
  function renderPalette(q) {
    var items = PAL.filter(function (p) { return !q || p.label.toLowerCase().indexOf(q.toLowerCase()) > -1; });
    var el = $('#paletteList');
    el.innerHTML = items.length ? items.map(function (p, i) {
      return '<li data-i="' + i + '"' + (i === 0 ? ' class="on"' : '') + '>' + Charts.esc(p.label) +
        '<span class="p-kind">' + p.kind + '</span></li>';
    }).join('') : '<li class="muted">没有匹配项</li>';
    el._items = items;
  }
  function palettePick(i) {
    var items = $('#paletteList')._items || [];
    if (items[i]) { $('#palette').hidden = true; items[i].run(); }
  }

  function togglePresent() {
    document.body.classList.toggle('present');
    $('#demoModeBtn').classList.toggle('on', document.body.classList.contains('present'));
    toast(document.body.classList.contains('present') ? '已进入演示模式（按 D 退出）' : '已退出演示模式');
  }

  /* ── 全局事件 ─────────────────────────────────────────────────────── */
  function bindGlobal() {
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-nav],[data-go],[data-copy],[data-ev],[data-dismiss],[data-run],[data-open-run],[data-hash]');
      if (!t) return;
      if (t.hasAttribute('data-nav')) { go(t.getAttribute('data-nav')); return; }
      if (t.hasAttribute('data-open-run')) { setRun(t.getAttribute('data-open-run')); return; }
      if (t.hasAttribute('data-go')) { go(t.getAttribute('data-go')); return; }
      if (t.hasAttribute('data-run')) { setRun(t.getAttribute('data-run')); return; }
      if (t.hasAttribute('data-copy')) { copy(t.getAttribute('data-copy')); return; }
      if (t.hasAttribute('data-retry')) { location.reload(); return; }
      if (t.hasAttribute('data-dismiss')) { t.closest('.banner').remove(); return; }
      if (t.hasAttribute('data-ev')) {
        var body = '';
        try { body = decodeURIComponent(t.getAttribute('data-ev-body') || ''); } catch (err) { body = ''; }
        openDrawer(t.getAttribute('data-ev-title') || '证据', '<div class="prose">' + body + '</div>');
        return;
      }
    });

    document.addEventListener('click', function (e) {
      var b = e.target.closest('#rpBtn');
      var menu = $('#rpMenu');
      if (b) { menu.hidden = !menu.hidden; return; }
      if (!e.target.closest('.run-picker')) menu.hidden = true;
    });

    /* 点别处就收起表格复制菜单 */
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#copyMenu') && !e.target.closest('[data-copy-table]')) closeCopyMenu();
    });

    $('#sidebarToggle').addEventListener('click', function () {
      document.body.classList.toggle('mini');
    });
    $('#navToggle').addEventListener('click', function () {
      document.body.classList.toggle('nav-open');
    });
    $('#demoModeBtn').addEventListener('click', togglePresent);
    $('#runBtn').addEventListener('click', function () { triggerRun({}); });
    $('#scrim').addEventListener('click', closeDrawer);
    $('#drawerClose').addEventListener('click', closeDrawer);
    $('#drawerCopy').addEventListener('click', function () {
      copy($('#drawerBody').innerText || '');
    });
    $('#lightboxClose').addEventListener('click', function () { $('#lightbox').hidden = true; });

    var inp = $('#paletteInput');
    inp.addEventListener('input', function () { renderPalette(inp.value); });
    inp.addEventListener('keydown', function (e) {
      var lis = Array.prototype.slice.call($('#paletteList').children);
      var idx = lis.findIndex(function (l) { return l.classList.contains('on'); });
      if (e.key === 'ArrowDown') { e.preventDefault(); if (idx > -1) lis[idx].classList.remove('on'); lis[Math.min(lis.length - 1, idx + 1)].classList.add('on'); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (idx > 0) { lis[idx].classList.remove('on'); lis[idx - 1].classList.add('on'); } }
      else if (e.key === 'Enter') { e.preventDefault(); palettePick(Math.max(0, idx)); }
      else if (e.key === 'Escape') { $('#palette').hidden = true; }
    });
    $('#paletteList').addEventListener('click', function (e) {
      var li = e.target.closest('li[data-i]');
      if (li) palettePick(Number(li.getAttribute('data-i')));
    });

    document.addEventListener('keydown', function (e) {
      var typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); openPalette(); return;
      }
      if (e.key === 'Escape') {
        $('#palette').hidden = true; closeDrawer(); $('#lightbox').hidden = true;
        closeCopyMenu();
        document.body.classList.remove('nav-open');
        return;
      }
      if (typing) return;
      if (e.key.toLowerCase() === 'd') { togglePresent(); }
      else if (e.key.toLowerCase() === 'r') { triggerRun({}); }
      else if (e.key === '[') { document.body.classList.toggle('mini'); }
      else if (e.key === '/') { e.preventDefault(); openPalette(); }
    });

    window.addEventListener('hashchange', function () {
      var h = parseHash();
      State.route = h.id;
      if (h.run && h.run !== State.runId) { State.runId = h.run; State.dataRun = undefined; }
      closeCopyMenu();
      document.body.classList.remove('nav-open');
      paint();
    });
  }

  /* ── 启动 ─────────────────────────────────────────────────────────── */
  function boot() {
    var h = parseHash();
    State.route = h.id;
    State.runId = h.run;
    bindGlobal();
    paintStaticIcons();
    renderNav();
    $('#view').innerHTML = '<div class="grid g-4">' +
      '<div class="skel skel-card"></div><div class="skel skel-card"></div>' +
      '<div class="skel skel-card"></div><div class="skel skel-card"></div></div>' +
      '<div class="skel skel-card mt14" style="height:240px"></div>';
    bootStatus().then(function () { return paint(); });
  }

  /* 启动时读状态：失败重试两次再放弃。
     以前失败一次就静默退回演示数据、而且不再重试，界面上几乎看不出来，
     录制演示时很容易把演示数字当成真实结果。 */
  function bootStatus(attempt) {
    attempt = attempt || 0;
    return API.status().then(function (s) {
      State.status = s;
      State.lastRunning = !!s.running;
      if (!State.runId && s.last_run) State.runId = s.last_run;
      if (s.running) schedulePoll(1200);
    }).catch(function () {
      if (attempt < 2) {
        return new Promise(function (r) { setTimeout(r, 500 * (attempt + 1)); })
          .then(function () { return bootStatus(attempt + 1); });
      }
      State.status = { environment: {}, recent: [] };
      State.runId = null;
      State.dataRun = undefined;
      toast('没能连上本地服务，当前显示的是内置演示数据', 'err');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
