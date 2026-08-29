/*
 * Injects the shared SVG symbol sheet as the first node in <body>.
 * Loaded as a render-blocking classic script so that every <use> in the static
 * markup resolves on the first paint (the page CSP forbids inline scripts).
 */
(function () {
  var symbols = {
    dashboard: '<rect x="3.2" y="3.2" width="7.4" height="7.4" rx="1.9"/><rect x="13.4" y="3.2" width="7.4" height="7.4" rx="1.9"/><rect x="3.2" y="13.4" width="7.4" height="7.4" rx="1.9"/><rect x="13.4" y="13.4" width="7.4" height="7.4" rx="1.9"/>',
    record: '<circle cx="12" cy="12" r="8.4"/><circle cx="12" cy="12" r="3.3" class="ic-solid"/>',
    video: '<rect x="3.2" y="4.8" width="17.6" height="14.4" rx="3.2"/><path d="m10.4 9.4 4.9 2.6-4.9 2.6z"/>',
    cloud: '<path d="M12 13v8"/><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="m8 17 4-4 4 4"/>',
    settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    menu: '<path d="M4.4 7.2h15.2M4.4 12h15.2M4.4 16.8h15.2"/>',
    refresh: '<path d="M20.4 12a8.4 8.4 0 1 1-2.5-6"/><path d="M20.6 4.6v5.6H15"/>',
    rotateCcw: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    plus: '<path d="M12 5.4v13.2M5.4 12h13.2"/>',
    logout: '<path d="M10.2 4.6H6.7A2.1 2.1 0 0 0 4.6 6.7v10.6a2.1 2.1 0 0 0 2.1 2.1h3.5"/><path d="m15.2 8.4 3.6 3.6-3.6 3.6M18.8 12H9.4"/>',
    chevronRight: '<path d="m9.6 5.6 6.4 6.4-6.4 6.4"/>',
    chevronLeft: '<path d="m14.4 5.6-6.4 6.4 6.4 6.4"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    close: '<path d="M6.6 6.6 17.4 17.4M17.4 6.6 6.6 17.4"/>',
    search: '<circle cx="11" cy="11" r="6.4"/><path d="m15.9 15.9 4.5 4.5"/>',
    folder: '<path d="M3.4 7.6a2.1 2.1 0 0 1 2.1-2.1h3.3l2 2.5h7.7a2.1 2.1 0 0 1 2.1 2.1v8.3a2.1 2.1 0 0 1-2.1 2.1H5.5a2.1 2.1 0 0 1-2.1-2.1z"/>',
    play: '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    pause: '<path d="M9.4 6.4v11.2M14.6 6.4v11.2"/>',
    download: '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    upload: '<path d="M12 19.8V9.2m-4 3.7 4-3.7 4 3.7"/><path d="M5.2 4.6h13.6"/>',
    expand: '<path d="M9.2 4.6H4.6v4.6M14.8 4.6h4.6v4.6M19.4 14.8v4.6h-4.6M4.6 14.8v4.6h4.6"/>',
    collapse: '<path d="M9.2 9.2V4.6M9.2 9.2H4.6M14.8 9.2V4.6M14.8 9.2h4.6M14.8 14.8v4.6M14.8 14.8h4.6M9.2 14.8v4.6M9.2 14.8H4.6"/>',
    checkCircle: '<circle cx="12" cy="12" r="8.4"/><path d="m8.4 12.2 2.5 2.5 4.7-5.1"/>',
    alert: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.6v5.2M12 16.2h.01"/>',
    info: '<circle cx="12" cy="12" r="8.4"/><path d="M12 11.2v5M12 8h.01"/>',
    sun: '<circle cx="12" cy="12" r="3.9"/><path d="M12 2.9v2M12 19.1v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.9 12h2M19.1 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    moon: '<path d="M20.1 14.3A8.4 8.4 0 0 1 9.7 3.9a8.5 8.5 0 1 0 10.4 10.4z"/>',
    monitor: '<rect x="3.2" y="4.4" width="17.6" height="11.8" rx="2.2"/><path d="M8.6 20.2h6.8M12 16.2v4"/>',
    trash: '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    edit: '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>',
    stop: '<rect width="18" height="18" x="3" y="3" rx="2"/><line x1="10" x2="10" y1="15" y2="9"/><line x1="14" x2="14" y1="15" y2="9"/>',
    disk: '<ellipse cx="12" cy="6.2" rx="7.2" ry="2.9"/><path d="M4.8 6.2v11.6c0 1.6 3.22 2.9 7.2 2.9s7.2-1.3 7.2-2.9V6.2"/><path d="M4.8 12c0 1.6 3.22 2.9 7.2 2.9s7.2-1.3 7.2-2.9"/>',
    clock: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.2V12l3.1 1.9"/>',
    calendar: '<rect x="3.6" y="5.2" width="16.8" height="15.2" rx="2.4"/><path d="M3.6 10h16.8M8.4 3.4v3.4M15.6 3.4v3.4"/>',
    pulse: '<path d="M3 12h3.6l2.3-6.4 4.6 12.8 2.4-6.4H21"/>',
    volume: '<path d="M11.2 5.4 6.9 9H4.4a.8.8 0 0 0-.8.8v4.4a.8.8 0 0 0 .8.8h2.5l4.3 3.6z"/><path d="M15.2 9.6a3.6 3.6 0 0 1 0 4.8M17.8 7a7.2 7.2 0 0 1 0 10"/>',
    volumeOff: '<path d="M11.2 5.4 6.9 9H4.4a.8.8 0 0 0-.8.8v4.4a.8.8 0 0 0 .8.8h2.5l4.3 3.6z"/><path d="m15.4 9.8 4.6 4.4M20 9.8l-4.6 4.4"/>',
    link: '<path d="M10.2 13.8a3.7 3.7 0 0 0 5.5.4l2.6-2.6a3.7 3.7 0 0 0-5.2-5.2l-1.5 1.5"/><path d="M13.8 10.2a3.7 3.7 0 0 0-5.5-.4l-2.6 2.6a3.7 3.7 0 0 0 5.2 5.2l1.5-1.5"/>',
    sort: '<path d="M7.4 4.8v14.4m0 0-3-3m3 3 3-3"/><path d="M16.6 19.2V4.8m0 0-3 3m3-3 3 3"/>',
    shield: '<path d="M12 3.4 5.2 6.1v5c0 4.3 2.9 8.2 6.8 9.4 3.9-1.2 6.8-5.1 6.8-9.4v-5z"/><path d="m9.3 11.9 2 2 3.4-3.6"/>',
    key: '<circle cx="8.2" cy="15.8" r="3.6"/><path d="m10.8 13.2 7.4-7.4M15.6 8.4l2 2M18 6l2 2"/>',
    user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    eye: '<path d="M3.3 12.3a.9.9 0 0 1 0-.6c1.2-2.7 4.3-6 8.7-6s7.5 3.3 8.7 6a.9.9 0 0 1 0 .6c-1.2 2.7-4.3 6-8.7 6s-7.5-3.3-8.7-6z"/><circle cx="12" cy="12" r="3.1"/>',
    eyeOff: '<path d="M10.7 6a9.8 9.8 0 0 1 1.3-.1c4.4 0 7.5 3.3 8.7 6a.9.9 0 0 1 0 .6 10.7 10.7 0 0 1-1.7 2.6"/><path d="M14.2 14.2a3.1 3.1 0 0 1-4.4-4.4"/><path d="M6.5 7.3a10.9 10.9 0 0 0-3.2 4.4.9.9 0 0 0 0 .6c1.2 2.7 4.3 6 8.7 6a9.7 9.7 0 0 0 3.9-.8"/><path d="m4.3 4.3 15.4 15.4"/>',
    layers: '<path d="m12 3.6 8.4 4.2-8.4 4.2L3.6 7.8z"/><path d="m3.6 12 8.4 4.2 8.4-4.2M3.6 16.2l8.4 4.2 8.4-4.2"/>',
    logs: '<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
    panelLeft: '<rect x="3.4" y="4.4" width="17.2" height="15.2" rx="2.6"/><path d="M9.6 4.4v15.2"/>',
    server: '<rect x="3.4" y="4.2" width="17.2" height="6.4" rx="2"/><rect x="3.4" y="13.4" width="17.2" height="6.4" rx="2"/><path d="M7.2 7.4h.01M7.2 16.6h.01"/>',
    cpu: '<rect x="6.4" y="6.4" width="11.2" height="11.2" rx="2.2"/><path d="M10.2 10.2h3.6v3.6h-3.6z"/><path d="M9.6 3.2v3.2M14.4 3.2v3.2M9.6 17.6v3.2M14.4 17.6v3.2M3.2 9.6h3.2M3.2 14.4h3.2M17.6 9.6h3.2M17.6 14.4h3.2"/>',
    check: '<path d="m5.4 12.6 4.2 4.2 9-9.6"/>',
    filter: '<path d="M4.2 5.6h15.6l-6 7.1v5.5l-3.6 1.8v-7.3z"/>',
    copy: '<rect x="9" y="9" width="11.4" height="11.4" rx="2.2"/><path d="M15 9V5.8a2.2 2.2 0 0 0-2.2-2.2H5.8a2.2 2.2 0 0 0-2.2 2.2v7a2.2 2.2 0 0 0 2.2 2.2H9"/>',
    playCircle: '<circle cx="12" cy="12" r="8.4"/><path d="m10.2 8.8 5.4 3.2-5.4 3.2z" class="ic-solid"/>',
    pauseCircle: '<circle cx="12" cy="12" r="8.4"/><path d="M10.2 9.2v5.6M13.8 9.2v5.6"/>',
  };

  var parts = ['<svg id="icon-sprite" aria-hidden="true" focusable="false">'];
  for (var name in symbols) {
    if (Object.prototype.hasOwnProperty.call(symbols, name)) {
      parts.push('<symbol id="ic-' + name + '" viewBox="0 0 24 24">' + symbols[name] + "</symbol>");
    }
  }
  parts.push("</svg>");
  document.body.insertAdjacentHTML("afterbegin", parts.join(""));

  /*
   * The markup helper lives here rather than in icons.js so that shell.js — a
   * classic script that cannot import modules — can build icons too. icons.js
   * re-exports this for the page modules.
   */
  window.streamKeeperIcon = function (name, className) {
    return (
      '<svg class="ic' + (className ? " " + className : "") +
      '" aria-hidden="true" focusable="false"><use href="#ic-' + name + '"/></svg>'
    );
  };
})();
