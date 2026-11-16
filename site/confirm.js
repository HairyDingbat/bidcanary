(function () {
  // Confirmation needs a button press, so email security scanners that open links can't confirm on someone's behalf.
  var cfg = window.BC_CONFIG || {}, msg = document.getElementById('msg'), go = document.getElementById('go');
  var t = new URLSearchParams(location.search).get('t');
  if (!t || !/^[0-9a-f-]{36}$/i.test(t)) { go.disabled = true; msg.textContent = 'This link is incomplete. Please use the link in your email.'; return; }
  go.addEventListener('click', function () {
    if (!cfg.supabaseUrl) { msg.textContent = 'Preview only.'; return; }
    go.disabled = true; msg.textContent = 'Confirming\u2026';
    fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/rpc/confirm_subscription', {
      method: 'POST',
      headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
      body: JSON.stringify({token: t})
    }).then(function (r) { return r.json(); }).then(function (ok) {
      go.style.display = 'none';
      msg.textContent = ok ? 'Thanks, you\u2019re confirmed. Your first tender of the week arrives on Monday.'
        : 'This link has already been used, so you\u2019re most likely confirmed already. If you don\u2019t get an email on Monday, email support@bidcanary.co.uk.';
    }).catch(function () { go.disabled = false; msg.textContent = 'Something went wrong. Please try again.'; });
  });
})();
