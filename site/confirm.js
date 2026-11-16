(function () {
  var cfg = window.BC_CONFIG || {}, msg = document.getElementById('msg');
  var t = new URLSearchParams(location.search).get('t');
  if (!t || !/^[0-9a-f-]{36}$/i.test(t)) { msg.textContent = 'This link is incomplete. Please use the link in your email.'; return; }
  if (!cfg.supabaseUrl) { msg.textContent = 'Preview only.'; return; }
  fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/rpc/confirm_subscription', {
    method: 'POST',
    headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
    body: JSON.stringify({token: t})
  }).then(function (r) { return r.json(); }).then(function (ok) {
    msg.textContent = ok ? 'Thanks, you’re confirmed. Your first digest arrives on Monday.' : 'This link has already been used or has expired. If you’re not getting digests, sign up again on the home page.';
  }).catch(function () { msg.textContent = 'Something went wrong. Please try the link again.'; });
})();
