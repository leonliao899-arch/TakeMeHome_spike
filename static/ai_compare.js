/* AI 照片比對（參考意見）前端共用：卡片 AI 區塊、黃框、輪詢。文字一律 textContent。
   AI 只是顯示：不改卡片順序，不影響「查看」或「就是這隻」。 */
window.TMH_AI = (function () {
  const NOTE = '僅供參考，請以照片與現場確認為準';

  function simClass(s) { return s === '高' ? 's-high' : s === '中' ? 's-mid' : 's-low'; }
  function mk(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // 更新一張卡片：info={similarity,reason}｜null；pending=true 顯示「比對中」。
  // host 是放 AI 區塊的容器（卡片內），card 是要加黃框的整張卡片。
  function apply(card, host, info, pending) {
    const old = host.querySelector(':scope > .ai-row');
    if (old) old.remove();
    card.classList.remove('ai-hit');
    if (info && info.similarity) {
      const row = mk('div', 'ai-row');
      const line = mk('div', 'ai-line');
      line.appendChild(mk('span', '', '🤖 辨識結果：'));
      line.appendChild(mk('span', 'ai-sim ' + simClass(info.similarity), info.similarity));
      if (info.reason) line.appendChild(mk('span', '', '・' + info.reason));
      row.appendChild(line);
      row.appendChild(mk('div', 'ai-note', NOTE));
      host.appendChild(row);
      if (info.similarity === '高' || info.similarity === '中') card.classList.add('ai-hit');
    } else if (pending) {
      host.appendChild(mk('div', 'ai-row ai-wait', '🤖 AI 比對中…'));
    }
  }

  // 詳情頁完整區塊（reason 不截斷）
  function box(info, pending) {
    if (!(info && info.similarity) && !pending) return null;
    const b = mk('div', 'ai-box');
    if (info && info.similarity) {
      const line = mk('div');
      line.appendChild(mk('span', '', '🤖 AI 參考意見：'));
      line.appendChild(mk('span', 'ai-sim ' + simClass(info.similarity), info.similarity));
      b.appendChild(line);
      if (info.reason) b.appendChild(mk('div', '', info.reason));
      b.appendChild(mk('div', 'ai-note', NOTE));
      if (info.similarity === '高' || info.similarity === '中') b.classList.add('ai-hit');
    } else {
      b.appendChild(mk('div', '', '🤖 AI 比對中…（稍後按「重新整理」）'));
    }
    return b;
  }

  // 每 2 秒呼叫 fetchOnce()（回傳是否還在 pending；丟錯視為結束），最多 25 秒；
  // 頁面切到背景就暫停，切回來繼續（總時間仍以 25 秒為限）。onEnd 在結束時呼叫一次。
  function poll(fetchOnce, onEnd) {
    const started = Date.now();
    let timer = null, stopped = false;
    function end() {
      if (stopped) return;
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVis);
      if (onEnd) onEnd();
    }
    async function tick() {
      timer = null;
      if (stopped || document.hidden) return;
      let more = false;
      try { more = await fetchOnce(); } catch (e) { more = false; }
      if (!more || Date.now() - started > 25000) return end();
      timer = setTimeout(tick, 2000);
    }
    function onVis() {
      if (stopped) return;
      if (document.hidden) { clearTimeout(timer); timer = null; }
      else if (!timer) {
        if (Date.now() - started > 25000) end(); else timer = setTimeout(tick, 500);
      }
    }
    document.addEventListener('visibilitychange', onVis);
    timer = setTimeout(tick, 1500);
    return end;
  }

  return { apply, box, poll, NOTE };
})();
