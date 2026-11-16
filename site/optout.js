(function () {
  var cfg = window.BC_CONFIG || {}, msg = document.getElementById('msg');
  var t = new URLSearchParams(location.search).get('t');
  if (!t || !/^[0-9a-f-]{36}$/i.test(t)) { msg.textContent = 'This link is incomplete. Reply to our email with "no thanks" and we will remove you.'; return; }
  if (!cfg.supabaseUrl) { msg.textContent = 'Preview only.'; return; }
  fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/rpc/outreach_optout', {
    method: 'POST',
    headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
    body: JSON.stringify({token: t})
  }).then(function (r) { return r.json(); }).then(function (ok) {
    msg.textContent = ok ? 'Done. We won’t email your company again.' : 'We couldn’t find that link. Reply to our email with "no thanks" and we will remove you.';
  }).catch(function () { msg.textContent = 'Something went wrong. Reply to our email with "no thanks" and we will remove you.'; });
})();
