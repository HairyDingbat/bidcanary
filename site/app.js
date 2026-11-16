(function () {
  if (window.top !== window.self) { window.top.location = window.self.location; }  // refuse to be framed
  var cfg = window.BC_CONFIG || {};
  if (cfg.cfAnalyticsToken) {  // cookie-free visit counts; see cookies.html
    var a = document.createElement('script'); a.defer = true; a.src = 'https://static.cloudflareinsights.com/beacon.min.js';
    a.setAttribute('data-cf-beacon', JSON.stringify({token: cfg.cfAnalyticsToken})); document.head.appendChild(a);
  }
  var PACKS = {cleaning: 'Cleaning', security: 'Security', grounds: 'Grounds maintenance', fm: 'Facilities management'};
  var REGIONS = {UKC:'North East',UKD:'North West',UKE:'Yorkshire and the Humber',UKF:'East Midlands',UKG:'West Midlands',UKH:'East of England',UKI:'London',UKJ:'South East',UKK:'South West',UKL:'Wales',UKM:'Scotland',UKN:'Northern Ireland'};
  function fill(sel, map, any) {
    if (any) sel.add(new Option(any, ''));
    Object.keys(map).forEach(function (k) { sel.add(new Option(map[k], k)); });
  }
  var cp = document.getElementById('c-pack'), cr = document.getElementById('c-region');
  var sp = document.getElementById('s-pack'), sr = document.getElementById('s-region');
  fill(cp, PACKS); fill(cr, REGIONS); fill(sp, PACKS); fill(sr, REGIONS, 'All of the UK');
  cr.value = 'UKD';
  var data = null, out = document.getElementById('c-out');
  function esc(s) { return String(s || '').replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function money(v) { return v == null ? 'Value not stated' : '£' + Math.round(v).toLocaleString('en-GB'); }
  function render() {
    if (!data) { out.innerHTML = '<p class="meta">Live counts will appear here once the service is running.</p>'; return; }
    var r = data.packs[cp.value].regions[cr.value];
    var h = '<div class="big">' + r.count + '</div><p style="margin:0 0 6px">open ' + esc(PACKS[cp.value].toLowerCase()) + ' tenders in ' + esc(REGIONS[cr.value]) + '.</p>' + (data.packs[cp.value].national ? '<p class="meta" style="margin:0 0 6px">Plus ' + data.packs[cp.value].national + ' UK-wide ' + esc(PACKS[cp.value].toLowerCase()) + ' tenders open to firms anywhere.</p>' : '');
    r.examples.forEach(function (e) {
      h += '<div class="ex"><b>' + esc(e.title) + '</b><span class="meta">' + esc(e.buyer) + ' · ' + money(e.value) + (e.deadline ? ' · Closes ' + esc(e.deadline) : '') + '</span></div>';
    });
    h += '<p style="margin:16px 0 0"><a class="btn btn-y" href="#signup">Get these by email, free</a></p>';
    h += '<p class="meta">Updated ' + esc(new Date(data.updated).toLocaleString('en-GB')) + '</p>';
    out.innerHTML = h;
    sp.value = cp.value; sr.value = cr.value;
  }
  cp.addEventListener('change', render); cr.addEventListener('change', render);
  fetch('data/counts.json', {cache: 'no-store'}).then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { data = d; render(); }).catch(function () { render(); });

  var form = document.getElementById('signup-form'), st = document.getElementById('s-status');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = document.getElementById('s-email').value.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { st.textContent = 'Please enter a valid email address.'; return; }
    if (document.getElementById('s-website').value) { st.textContent = 'Nearly done: check your inbox.'; return; }  // bot trap
    if (!document.getElementById('s-biz').checked) { st.textContent = 'Please confirm you\u2019re signing up for a business.'; return; }
    if (!cfg.supabaseUrl || !cfg.supabaseAnonKey) { st.textContent = 'Preview only: sign-ups open at launch.'; return; }
    st.textContent = 'Signing you up…';
    fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/subscribers', {
      method: 'POST',
      headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
      body: JSON.stringify({email: email, pack: sp.value, regions: sr.value, consent_text: 'Weekly digest requested on website; confirmed business use and accepted terms; ' + new Date().toISOString()})
    }).then(function (r) {
      if (r.ok) {
        st.textContent = 'Nearly done: we’ve emailed you a link. Click it to confirm, and your first digest arrives on Monday.';
      } else if (r.status === 409) {
        st.textContent = 'You’re already signed up with that email.';
      } else { st.textContent = 'Something went wrong. Please try again.'; }
    }).catch(function () { st.textContent = 'Something went wrong. Please try again.'; });
  });
  var sb = document.getElementById('starter-btn');
  if (cfg.starterLink) { sb.href = cfg.starterLink; sb.textContent = 'Start Starter'; }
})();
