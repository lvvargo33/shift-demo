'use strict';
// Adapted from Chris's member_preference.js (shift-survey-main-integration
// 2026-10-21, patch 0001). Same flow; Shift wording (ROADMAP Block 25 A1).
(() => {
  const params = new URLSearchParams(location.search);
  const token = params.get('token');
  const button = document.getElementById('opt-out');
  const status = document.getElementById('status');
  // Keep the bearer only in memory, never in storage or subsequent URL history.
  history.replaceState(null, '', location.pathname);
  if (button.hidden || !token) return;
  let requestId;
  const key = 'survey-opt-out-request:' + token.split('.')[0];
  try { requestId = sessionStorage.getItem(key); } catch (_) {}
  if (!requestId) {
    requestId = crypto.randomUUID();
    try { sessionStorage.setItem(key, requestId); } catch (_) {}
  }
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    status.textContent = 'Saving your choice…';
    try {
      const response = await fetch('/survey-preference/opt-out', {
        method: 'POST', credentials: 'omit', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
        body: JSON.stringify({request_id: requestId})
      });
      const result = await response.json();
      if (response.ok && result.preference === 'opted_out') {
        status.textContent = 'Done. You won’t get any more survey emails from Shift. Other emails from Shift are not affected.';
        button.hidden = true;
      } else if (response.status === 403) {
        status.textContent = 'This link is invalid or has expired.';
        button.hidden = true;
      } else {
        throw new Error('Save not confirmed');
      }
    } catch (_) {
      status.textContent = "We couldn't confirm the save. Please try again.";
      button.textContent = 'Try again';
      button.disabled = false;
    }
    status.focus();
  });
})();
