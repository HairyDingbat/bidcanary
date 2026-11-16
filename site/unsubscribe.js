(function () {
  var cfg = window.BC_CONFIG || {}, msg = document.getElementById('msg');
  var t = new URLSearchParams(location.search).get('t');
  if (!t || !/^[0-9a-f-]{36}$/i.test(t)) { msg.textContent = 'This link is incomplete. Reply to any digest email and we will remove you.'; return; }
  if (!cfg.supabaseUrl) { msg.textContent = 'Preview only.'; return; }
  fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/rpc/unsubscribe', {
    method: 'POST',
    headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
    body: JSON.stringify({token: t})
  }).then(function (r) { return r.json(); }).then(function (ok) {
    msg.textContent = ok ? 'Done. You won’t get any more Bid Canary digests.' : 'We couldn’t find that subscription; it may already be cancelled.';
  }).catch(function () { msg.textContent = 'Something went wrong. Reply to any digest email and we will remove you.'; });
})();
