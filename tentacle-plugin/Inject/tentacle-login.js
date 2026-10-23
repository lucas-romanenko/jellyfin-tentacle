/**
 * Tentacle Login — Jellyfin's login page in Tentacle's look, the same as
 * Tentacle's own sign-in screen: "Who's watching?" with round faces (a photo
 * or a coloured initial), then the password for the person picked, or "Sign
 * in with another account" / "Use a code" (Quick Connect).
 *
 * Only words and decoration change: picking, signing in, Quick Connect and
 * the error messages stay Jellyfin's own. Who is listed is Jellyfin's
 * /Users/Public, which the plugin empties for callers outside the home
 * network (PublicUsersFilter); Jellyfin then opens the name form by itself.
 */
(function () {
  'use strict';

  // The same colours, in the same order, as Tentacle's sign-in (static/js/app.js).
  var COLORS = ['#6c63ff', '#e0607e', '#2bb6a3', '#e3913b', '#4f8fe8', '#a35ee0', '#5bb05f', '#d0574a'];

  function colorFor(name) {
    var h = 0;
    var s = String(name || '');
    for (var i = 0; i < s.length; i++) {
      h = (h * 31 + s.codePointAt(i)) >>> 0;
      if (s.codePointAt(i) > 0xffff) i++;
    }
    return COLORS[h % COLORS.length];
  }

  function initialOf(name) {
    var first = Array.from(String(name || '?').trim())[0] || '?';
    return first.toUpperCase();
  }

  var picked = null;   // { name, color, image } of the face clicked, until Back

  // Every change below is made only when something differs: the page-wide
  // observer that calls this would otherwise be woken by its own writes.
  function setText(el, text) {
    if (!el) return;
    var target = el.querySelector('span') || el;
    if (target.textContent !== text) target.textContent = text;
  }

  function setDisplay(el, value) {
    if (el && el.style.display !== value) el.style.display = value;
  }

  // A face for one card of #divUsers: Jellyfin's square tile becomes a circle
  // in the person's colour, with their initial unless Jellyfin has a photo.
  function decorateCard(card) {
    if (card.getAttribute('data-tentacle')) return;
    var content = card.querySelector('.cardContent');
    var image = card.querySelector('.cardImage, .cardImageContainer');
    if (!content || !image) return;
    var name = content.getAttribute('data-username') || '';
    card.setAttribute('data-tentacle', '1');
    var bg = image.style.backgroundImage;
    var hasPhoto = !!(bg && bg !== 'none');
    image.style.backgroundColor = colorFor(name);
    if (!hasPhoto) {
      var span = document.createElement('span');
      span.className = 'tentacle-initial';
      span.setAttribute('aria-hidden', 'true');
      span.textContent = initialOf(name);
      image.appendChild(span);
    }
    card.addEventListener('click', function () {
      picked = { name: name, color: colorFor(name), image: hasPhoto ? bg : '' };
    }, true);
  }

  // The password form: who is signing in (a face clicked) or a name to type.
  function renderForm(page) {
    var form = page.querySelector('.manualLoginForm');
    if (!form) return;
    var who = form.querySelector('.tentacle-login-who');
    if (!who) {
      who = document.createElement('div');
      who.className = 'tentacle-login-who';
      var title = form.querySelector('.sectionTitle');
      var anchor = title ? title.closest('div') : form.firstChild;
      form.insertBefore(who, anchor);
    }
    var nameBox = form.querySelector('#txtManualName');
    var nameField = nameBox ? nameBox.closest('.inputContainer') : null;
    var cancel = form.querySelector('.btnCancel');
    var nameNow = nameBox ? nameBox.value : '';
    var isPicked = !!(picked && nameNow && picked.name === nameNow);
    if (form.classList.contains('tentacle-picked') !== isPicked) form.classList.toggle('tentacle-picked', isPicked);
    var key = isPicked ? picked.name + '|' + picked.color + '|' + picked.image : '';
    if (who.getAttribute('data-key') === key) {
      key = null;   // drawn already
    } else {
      who.setAttribute('data-key', key);
    }
    if (key === null) {
      // nothing to redraw
    } else if (isPicked) {
      who.innerHTML = '';
      var face = document.createElement('div');
      face.className = 'tentacle-face';
      face.style.backgroundColor = picked.color;
      if (picked.image) {
        face.style.backgroundImage = picked.image;
      } else {
        face.textContent = initialOf(picked.name);
      }
      var label = document.createElement('div');
      label.className = 'tentacle-face-name';
      label.textContent = picked.name;
      who.appendChild(face);
      who.appendChild(label);
    } else {
      who.innerHTML = '';
    }
    setDisplay(nameField, isPicked ? 'none' : '');
    setText(form.querySelector('.sectionTitle'), 'Sign in');
    setText(form.querySelector('label[for="txtManualName"]'), 'Your name');
    setText(form.querySelector('.button-submit'), 'Sign in');
    // Without a list of faces (away from home) there is nothing to go back to.
    var faces = page.querySelectorAll('#divUsers .card').length;
    if (cancel) {
      setText(cancel, '← Back');
      if (cancel.classList.contains('tentacle-hide') !== !faces) cancel.classList.toggle('tentacle-hide', !faces);
    }
  }

  function decorate(page) {
    if (!page.classList.contains('tentacle-login')) page.classList.add('tentacle-login');
    var visual = page.querySelector('.visualLoginForm');
    if (visual && !visual.querySelector('.tentacle-login-head')) {
      var head = document.createElement('div');
      head.className = 'tentacle-login-head';
      head.innerHTML = '<img class="tentacle-login-brand" src="/Tentacle/Assets/brand-lockup.svg" alt="Tentacle">'
        + '<h1 class="tentacle-login-title">Who’s watching?</h1>'
        + '<p class="tentacle-login-sub">Pick yourself to sign in.</p>';
      visual.insertBefore(head, visual.firstChild);
    }
    page.querySelectorAll('#divUsers .card').forEach(decorateCard);
    setText(page.querySelector('.btnManual'), 'Sign in with another account');
    setText(page.querySelector('.btnQuick'), 'Use a code');
    setText(page.querySelector('.btnForgotPassword'), 'Forgot password?');
    var manual = page.querySelector('.btnManual');
    if (manual && !manual.getAttribute('data-tentacle')) {
      manual.setAttribute('data-tentacle', '1');
      manual.addEventListener('click', function () { picked = null; }, true);
    }
    var form = page.querySelector('.manualLoginForm');
    if (form && !form.getAttribute('data-tentacle')) {
      form.setAttribute('data-tentacle', '1');
      // Jellyfin shows and hides the form by its class; redraw it each time.
      new MutationObserver(function () { renderForm(page); })
        .observe(form, { attributes: true, attributeFilter: ['class'] });
      var cancel = form.querySelector('.btnCancel');
      if (cancel) cancel.addEventListener('click', function () { picked = null; }, true);
    }
    renderForm(page);
  }

  // Jellyfin keeps an earlier login page in the document when it shows a new
  // one (signing out, a Quick Connect sign-in), both with id loginPage:
  // decorate every one, not just the first getElementById finds.
  function scan() {
    document.querySelectorAll('#loginPage').forEach(decorate);
  }

  var queued = false;
  new MutationObserver(function () {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; scan(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('viewshow', scan);
  scan();
})();
