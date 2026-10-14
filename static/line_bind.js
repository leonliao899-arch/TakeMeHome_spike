/**
 * 「用 LINE 追蹤」區塊（階段 3、4 共用）
 *
 * 飼主成功頁、協尋者成功頁、案件頁、追蹤頁都用這一支，行為才會一致：
 *   1. 按鈕是一個 <a>，網址是 https://line.me/R/oaMessage/{Basic ID}/?{綁定 token}，
 *      手機會開啟 LINE 並在官方帳號聊天室預填「綁定 own_xxx」。
 *   2. 點擊時「不等待」地把 token 複製到剪貼簿，讓 <a> 的跳轉照常進行
 *      （await 之後才跳轉，iOS 會把它當成非使用者操作而擋掉）。
 *   3. 畫面上同時顯示「綁定 xxx」整段文字＋複製鈕，LINE 沒帶入文字時可以手動貼上。
 *   4. 綁定狀態用 GET /api/bind-status/<token> 查：每 3 秒一次、最多約 2 分鐘；
 *      使用者從 LINE 切回瀏覽器（頁面重新可見）時會自動再查一輪。
 *
 * 用法：
 *   <script>window.LINE_BOT_BASIC_ID = {{ line_bot_basic_id|tojson }};</script>
 *   <script src="/static/line_bind.js"></script>
 *   TMH_LINE.render(容器元素, { token: 'own_xxx', kind: 'owner' });   // kind: 'owner' | 'sighting'
 * 沒有設定 LINE_BOT_BASIC_ID 時 render() 什麼都不畫、回傳 false，網頁其他功能照常。
 */
(function () {
  const POLL_INTERVAL_MS = 3000;
  const POLL_MAX_TIMES = 40;   // 3 秒 × 40 次 ≈ 2 分鐘

  const CSS = `
    .tmh-line { border: 1px solid #bbf7d0; background: #f0fdf4; border-radius: 10px;
      padding: 12px 14px; margin: 14px 0; text-align: left; font-weight: normal; color: #334155; }
    .tmh-line-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .tmh-line-title { font-weight: bold; font-size: 15px; color: #166534; }
    .tmh-line-chip { font-size: 12px; font-weight: bold; border-radius: 10px; padding: 2px 10px;
      white-space: nowrap; background: #e2e8f0; color: #475569; }
    .tmh-line-chip.checking { background: #dbeafe; color: #1d4ed8; }
    .tmh-line-chip.bound { background: #16a34a; color: #fff; }
    .tmh-line-desc { font-size: 13px; margin: 6px 0 10px; }
    .tmh-line-btn { display: block; text-align: center; padding: 12px; border-radius: 8px;
      background: #06c755; color: #fff !important; font-weight: bold; font-size: 16px;
      text-decoration: none; }
    .tmh-line-btn.secondary { background: #fff; color: #15803d !important; border: 1px solid #86efac;
      font-size: 14px; padding: 9px; }
    .tmh-line-manual { font-size: 12px; margin-top: 10px; color: #475569; }
    .tmh-line-code { display: flex; gap: 6px; align-items: stretch; margin-top: 4px; }
    .tmh-line-code code { flex: 1; min-width: 0; background: #fff; border: 1px solid #cbd5e1;
      border-radius: 6px; padding: 6px 8px; font-size: 12px; word-break: break-all;
      user-select: all; -webkit-user-select: all; }
    .tmh-line-code button, .tmh-line-recheck { font-size: 12px; padding: 6px 10px; border-radius: 6px;
      border: 1px solid #cbd5e1; background: #fff; color: #475569; cursor: pointer; white-space: nowrap; }
    .tmh-line-recheck { margin-top: 8px; width: 100%; }
    .tmh-line-tip { font-size: 12px; color: #b45309; margin-top: 6px; }
  `;

  function injectCss() {
    if (document.getElementById('tmh-line-css')) return;
    const style = document.createElement('style');
    style.id = 'tmh-line-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function basicId() {
    return String(window.LINE_BOT_BASIC_ID || '').trim();
  }

  /** LINE 的「傳訊息給官方帳號」網址：Basic ID 與預填文字都要百分比編碼（@ → %40） */
  function lineUrl(token) {
    return `https://line.me/R/oaMessage/${encodeURIComponent(basicId())}/?`
      + encodeURIComponent(`綁定 ${token}`);
  }

  /** 複製文字：不 await，失敗也不影響（部分 App 內建瀏覽器不允許剪貼簿） */
  function copyNoWait(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});
      }
    } catch (e) { /* 忽略 */ }
  }

  function render(container, opts) {
    const token = opts && opts.token;
    if (!container || !token || !basicId()) return false;
    injectCss();

    const isOwner = opts.kind !== 'sighting';
    const box = el('div', 'tmh-line');
    const head = el('div', 'tmh-line-head');
    head.appendChild(el('span', 'tmh-line-title', '📲 用 LINE 追蹤'));
    const chip = el('span', 'tmh-line-chip', '尚未綁定');
    head.appendChild(chip);
    box.appendChild(head);

    box.appendChild(el('div', 'tmh-line-desc', opts.preSubmit
      ? '按下按鈕會開啟 LINE，直接按「傳送」即可；綁定後回到這個頁面，再按「確認發布」。'
      : isOwner
      ? '綁定官方帳號後，就能從 LINE 聊天室隨時回到這個案件，不怕連結弄丟。按下按鈕會開啟 LINE，直接按「傳送」即可。'
      : '綁定官方帳號後，就能從 LINE 隨時查看這筆通報的處理進度。按下按鈕會開啟 LINE，直接按「傳送」即可。'));

    const btn = el('a', 'tmh-line-btn', '用 LINE 追蹤');
    btn.href = lineUrl(token);
    btn.rel = 'noopener';
    box.appendChild(btn);

    // 備案：LINE 沒有自動帶入文字時，手動貼上這段
    const manual = el('div', 'tmh-line-manual', 'LINE 沒有自動帶入文字？請在官方帳號聊天室貼上這段：');
    const codeRow = el('div', 'tmh-line-code');
    const bindText = `綁定 ${token}`;
    codeRow.appendChild(el('code', '', bindText));
    const copyBtn = el('button', '', '📋 複製');
    copyBtn.type = 'button';
    copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(bindText);
        copyBtn.textContent = '✅ 已複製';
      } catch (e) {
        copyBtn.textContent = '請長按文字複製';
      }
    };
    codeRow.appendChild(copyBtn);
    manual.appendChild(codeRow);
    box.appendChild(manual);

    const tip = el('div', 'tmh-line-tip', '');
    tip.style.display = 'none';
    box.appendChild(tip);

    const recheck = el('button', 'tmh-line-recheck', '🔄 我已經在 LINE 送出了，重新檢查');
    recheck.type = 'button';
    recheck.style.display = 'none';
    box.appendChild(recheck);

    container.appendChild(box);

    // ---------- 綁定狀態 ----------
    let bound = false;
    let pollTimer = null;
    let pollCount = 0;
    let userStarted = false;   // 按過按鈕才在切回頁面時自動輪詢

    function setChip(state) {
      chip.className = 'tmh-line-chip' + (state === 'unbound' ? '' : ' ' + state);
      chip.textContent = state === 'bound' ? '已綁定 LINE ✓'
        : state === 'checking' ? '等待綁定中…' : '尚未綁定';
    }

    function markBound() {
      bound = true;
      stopPolling();
      setChip('bound');
      recheck.style.display = 'none';
      tip.style.display = 'none';
      btn.className = 'tmh-line-btn secondary';
      btn.textContent = '開啟 LINE（換帳號可再綁一次）';
    }

    async function checkOnce() {
      try {
        const res = await fetch(`/api/bind-status/${encodeURIComponent(token)}`, { cache: 'no-store' });
        if (!res.ok) return false;
        const data = await res.json();
        if (data.bound) markBound();
        return !!data.bound;
      } catch (e) {
        return false;   // 網路不穩：下一輪再查
      }
    }

    function stopPolling() {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = null;
    }

    function startPolling() {
      if (bound) return;
      stopPolling();
      pollCount = 0;
      setChip('checking');
      recheck.style.display = 'none';
      tip.style.display = 'none';
      const tick = async () => {
        pollTimer = null;
        if (await checkOnce()) return;
        pollCount += 1;
        if (pollCount >= POLL_MAX_TIMES) {
          setChip('unbound');
          tip.textContent = '還沒收到綁定。請確認已在 LINE 按下「傳送」，再按下方按鈕重新檢查。';
          tip.style.display = 'block';
          recheck.style.display = 'block';
          return;
        }
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
      };
      tick();
    }

    btn.addEventListener('click', () => {
      copyNoWait(token);      // 不 await：讓 <a> 的導覽照常進行
      userStarted = true;
      startPolling();
    });
    recheck.onclick = startPolling;

    // 從 LINE 切回瀏覽器時，手機常會暫停背景計時器：頁面重新可見就再查一輪
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && userStarted && !bound) startPolling();
    });

    checkOnce();   // 一進頁面先查一次（案件頁、追蹤頁可能早就綁好了）
    return true;
  }

  window.TMH_LINE = { render, lineUrl };
})();
