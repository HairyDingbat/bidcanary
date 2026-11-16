(function () {
  if (window.top !== window.self) { window.top.location = window.self.location; }  // refuse to be framed
  var cfg = window.BC_CONFIG || {};
  if (cfg.cfAnalyticsToken) {  // cookie-free visit counts; see cookies.html
    var a = document.createElement('script'); a.defer = true; a.src = 'https://static.cloudflareinsights.com/beacon.min.js';
    a.setAttribute('data-cf-beacon', JSON.stringify({token: cfg.cfAnalyticsToken})); document.head.appendChild(a);
  }
  var PACKS = {"cleaning": "Cleaning", "security": "Security", "grounds": "Grounds maintenance", "fm": "Facilities management", "construction": "Construction and building works", "engineering": "Architecture, engineering and surveying", "repairs": "Building maintenance and trades", "it": "IT, software and digital", "health": "Health and social care", "medical": "Medical equipment and supplies", "education": "Education and training", "transport": "Transport, fleet and vehicles", "catering": "Catering and food", "professional": "Consultancy and professional services", "marketing": "Marketing, design and communications", "staffing": "Recruitment and staffing", "waste": "Waste and environmental services", "energy": "Energy and utilities", "supplies": "Furniture, equipment and supplies", "custom": "Something else (use my keywords)"};  // keep in step with engine.PACKS
  function phrase(k) { var l = PACKS[k] || ''; return /^[A-Z]{2}/.test(l) ? l : l.charAt(0).toLowerCase() + l.slice(1); }
  var CHECK = {}; Object.keys(PACKS).forEach(function (k) { if (k !== 'custom') CHECK[k] = PACKS[k]; });
  var REGIONS = {UKC:'North East',UKD:'North West',UKE:'Yorkshire and the Humber',UKF:'East Midlands',UKG:'West Midlands',UKH:'East of England',UKI:'London',UKJ:'South East',UKK:'South West',UKL:'Wales',UKM:'Scotland',UKN:'Northern Ireland'};
  function fill(sel, map, any) {
    if (any) sel.add(new Option(any, ''));
    Object.keys(map).forEach(function (k) { sel.add(new Option(map[k], k)); });
  }
  var cp = document.getElementById('c-pack'), cr = document.getElementById('c-region');
  var sp = document.getElementById('s-pack'), sr = document.getElementById('s-region');
  fill(cp, CHECK); fill(cr, REGIONS); fill(sp, PACKS); fill(sr, REGIONS, 'All of the UK');
  var kw = document.getElementById('s-kw'), kwWrap = document.getElementById('s-kw-wrap');
  function kwState() { kwWrap.querySelector('.meta').textContent = sp.value === 'custom' ? 'Required: we match tenders by these words.' : 'Optional: extra words to catch, e.g. \u201cwindow cleaning, deep clean\u201d.'; }
  sp.addEventListener('change', kwState); kwState();
  cr.value = 'UKD';
  var pre = new URLSearchParams(location.search).get('sector');
  if (pre && CHECK[pre]) { cp.value = pre; sp.value = pre; }
  var data = null, out = document.getElementById('c-out');
  function esc(s) { return String(s || '').replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function money(v) { return v == null ? 'Value not stated' : '£' + Math.round(v).toLocaleString('en-GB'); }
  function render() {
    if (!data) { out.innerHTML = '<p class="meta">Live counts will appear here once the service is running.</p>'; return; }
    if (!data.packs[cp.value]) { out.innerHTML = '<p class="meta">Live counts for this sector appear after the next update.</p>'; return; }
    var r = data.packs[cp.value].regions[cr.value];
    var h = '<div class="big">' + r.count + '</div><p style="margin:0 0 6px">open ' + esc(phrase(cp.value)) + ' tenders in ' + esc(REGIONS[cr.value]) + '.</p>' + (data.packs[cp.value].national ? '<p class="meta" style="margin:0 0 6px">Plus ' + data.packs[cp.value].national + ' UK-wide ' + esc(phrase(cp.value)) + ' tenders open to firms anywhere.</p>' : '');
    r.examples.forEach(function (e) {
      h += '<div class="ex"><b>' + esc(e.title) + '</b><span class="meta">' + esc(e.buyer) + ' · ' + money(e.value) + (e.deadline ? ' · Closes ' + esc(e.deadline) : '') + '</span></div>';
    });
    h += '<p style="margin:16px 0 0"><a class="btn btn-y" href="#signup">Get these by email, free</a></p>';
    h += '<p class="meta">Updated ' + esc(new Date(data.updated).toLocaleString('en-GB')) + '</p>';
    out.innerHTML = h;
    sp.value = cp.value; sr.value = cr.value; kwState();
  }
  cp.addEventListener('change', render); cr.addEventListener('change', render);
  fetch('data/counts.json', {cache: 'no-store'}).then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { data = d; render(); }).catch(function () { render(); });

  // Referral code from a customer's link (?ref=abcd1234); kept for this visit only.
  var ref = null;
  try {
    var r0 = new URLSearchParams(location.search).get('ref');
    if (r0 && /^[a-f0-9]{8}$/.test(r0)) sessionStorage.setItem('bc_ref', r0);
    ref = sessionStorage.getItem('bc_ref');
  } catch (e) { ref = null; }
  if (ref && !/^[a-f0-9]{8}$/.test(ref)) ref = null;
  var form = document.getElementById('signup-form'), st = document.getElementById('s-status');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = document.getElementById('s-email').value.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { st.textContent = 'Please enter a valid email address.'; return; }
    if (document.getElementById('s-website').value) { st.textContent = 'Nearly done: check your inbox.'; return; }  // bot trap
    var kws = kw.value.trim().slice(0, 200);
    if (sp.value === 'custom' && kws.length < 3) { st.textContent = 'Please add a few keywords so we know what to look for.'; return; }
    if (!document.getElementById('s-biz').checked) { st.textContent = 'Please confirm you\u2019re signing up for a business.'; return; }
    if (!cfg.supabaseUrl || !cfg.supabaseAnonKey) { st.textContent = 'Preview only: sign-ups open at launch.'; return; }
    st.textContent = 'Signing you up…';
    fetch(cfg.supabaseUrl.replace(/\/$/, '') + '/rest/v1/subscribers', {
      method: 'POST',
      headers: Object.assign({'apikey': cfg.supabaseAnonKey, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}, /^sb_/.test(cfg.supabaseAnonKey) ? {} : {'Authorization': 'Bearer ' + cfg.supabaseAnonKey}),
      body: JSON.stringify(Object.assign({email: email, pack: sp.value, regions: sr.value}, kws ? {keywords: kws} : {}, ref ? {referred_by: ref} : {}, {consent_text: '14-day trial then weekly digest requested on website; confirmed business use and accepted terms; ' + new Date().toISOString()}))
    }).then(function (r) {
      if (r.ok) {
        st.textContent = 'Nearly done: we’ve emailed you a link. Click it to confirm, and your 14-day trial starts with tomorrow morning’s digest.';
      } else if (r.status === 409) {
        st.textContent = 'You’re already signed up with that email.';
      } else { st.textContent = 'Something went wrong. Please try again.'; }
    }).catch(function () { st.textContent = 'Something went wrong. Please try again.'; });
  });
  var sb = document.getElementById('starter-btn');
  if (cfg.starterLink) { sb.href = cfg.starterLink; sb.textContent = 'Pay monthly: £29.99'; }
  var ab = document.getElementById('starter-annual-btn');
  if (cfg.starterAnnualLink) { ab.href = cfg.starterAnnualLink; ab.hidden = false; }
})();
