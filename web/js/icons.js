/* 图标集：内联 SVG，零外部依赖。
   用法：Icons.svg('dashboard')  或  Icons.svg('close','nav-ico') */
(function (global) {
  'use strict';

  var P = {
    dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    trend: '<path d="M3 17l6-6 4 4 8-8"/><path d="M21 11V7h-4"/>',
    sliders: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    scale: '<path d="M12 4v16M7 20h10"/><path d="M6 8h12"/><path d="M6 8l-3 6h6zM18 8l-3 6h6z"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
    shield: '<path d="M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
    fingerprint: '<path d="M12 4c-4.4 0-8 3.6-8 8"/><path d="M20 12c0-4.4-3.6-8-8-8"/><path d="M8 12a4 4 0 0 1 8 0"/><path d="M12 12v5"/><path d="M9 17.5c.6-1 1.8-1.6 3-1.5"/><path d="M5 15.5c.4 1.6 1.3 3 2.6 4"/><path d="M19 15.5c-.4 1.6-1.3 3-2.6 4"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4.5l3 1.8"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 4.6 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 11.5 4a2 2 0 1 1 4 0 1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9 2 2 0 1 1 0 4z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    bell: '<path d="M18 15V10a6 6 0 1 0-12 0v5l-1.5 2.5h15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    chevronDown: '<path d="M6 9l6 6 6-6"/>',
    chevronRight: '<path d="M9 6l6 6-6 6"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4l-8 8"/><path d="M18 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
    alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8.2v.01"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
    play: '<path d="M8 5l11 7-11 7z"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    download: '<path d="M12 4v11"/><path d="M7.5 11L12 15.5 16.5 11"/><path d="M4 19h16"/>',
    upRight: '<path d="M7 17L17 7"/><path d="M9 7h8v8"/>',
    downRight: '<path d="M7 7l10 10"/><path d="M17 9v8H9"/>',
    database: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    sparkles: '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6z"/><path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
    hash: '<path d="M9 4L7.5 20M16.5 4L15 20M4 9h16M3 15h16"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l2.5 2.5L7 14M12.5 15H17"/>',
    gitBranch: '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/><circle cx="17" cy="12" r="2.2"/><path d="M7 8.2v7.6M9.2 6H13a4 4 0 0 1 4 4v0"/>',
    layers: '<path d="M12 3l8 4.5-8 4.5-8-4.5z"/><path d="M4 12.5L12 17l8-4.5"/><path d="M4 16.5L12 21l8-4.5"/>',
    filter: '<path d="M3 5h18l-7 8v6l-4-2v-4z"/>',
    eye: '<path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/>',
    lock: '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3"/>',
    listChecks: '<path d="M4 7l2 2 3-3"/><path d="M4 16l2 2 3-3"/><path d="M12 8h8M12 17h8"/>',
    book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M8 7h7M8 11h7"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3.2 2"/>',
    percent: '<path d="M19 5L5 19"/><circle cx="7.5" cy="7.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="16" rx="2"/><path d="M3.5 10h17M8 3.5v3M16 3.5v3"/>',
    table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 10h17M3.5 14.5h17M9.5 10v9.5"/>',
    save: '<path d="M5 5h11l3 3v11H5z"/><path d="M9 5v5h6V5M9 19v-5h6v5"/>',
    rocket: '<path d="M14 4c3.5 0 6 2.5 6 6 0 5-6 10-6 10s-6-5-6-10c0-3.5 2.5-6 6-6z"/><circle cx="14" cy="10" r="1.6"/><path d="M8 15l-3 3M8 9L4 7"/>',
    moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
    dot: '<circle cx="12" cy="12" r="4"/>'
  };

  function svg(name, cls) {
    var body = P[name] || P.dot;
    return '<svg class="' + (cls || '') + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true" focusable="false">' + body + '</svg>';
  }

  global.Icons = { svg: svg, has: function (n) { return !!P[n]; } };
})(window);
