"""
DEMASCAS — core/screen/dom.py
The web page in the front Brave window as numbered elements: every button,
link, field, checkbox, radio and dropdown that is visible, with its label
(aria-label, <label for>, wrapping <label>, placeholder), its state, and the
form question it belongs to.  Elements are tagged data-dmx-id="N" so an
action can find exactly that element again.

Password fields are listed (so DEMASCAS knows not to touch them) but their
value is never read.  Read-only: observing changes nothing but the tag.
"""

from __future__ import annotations

import json

OBSERVE_JS = r"""
(function(){
  var MAX = 150, out = [], n = 0;
  var old = document.querySelectorAll('[data-dmx-id]');
  for (var k = 0; k < old.length; k++) old[k].removeAttribute('data-dmx-id');
  function txt(s){ return (s || '').replace(/\s+/g, ' ').trim().slice(0, 90); }
  function shown(el){
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return false;
    return r.bottom > -300 && r.top < innerHeight + 900;
  }
  function label(el){
    var a = el.getAttribute('aria-label'); if (a && txt(a)) return txt(a);
    var lb = el.getAttribute('aria-labelledby');
    if (lb) { var t = lb.split(' ').map(function(id){ var x = document.getElementById(id); return x ? x.innerText : ''; }).join(' ');
              if (txt(t)) return txt(t); }
    if (el.id) { try { var f = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (f && txt(f.innerText)) return txt(f.innerText); } catch(e) {} }
    var p = el.closest('label'); if (p && txt(p.innerText)) return txt(p.innerText);
    var tag = el.tagName;
    if (tag === 'INPUT' && /^(submit|button|reset)$/i.test(el.type)) return txt(el.value);
    return txt(el.innerText || el.placeholder || el.title || el.alt || el.getAttribute('data-tooltip') || el.name || '');
  }
  function role(el){
    var r = (el.getAttribute('role') || '').toLowerCase(), t = el.tagName.toLowerCase(), ty = (el.type || '').toLowerCase();
    if (r === 'checkbox' || r === 'switch' || ty === 'checkbox') return 'checkbox';
    if (r === 'radio' || ty === 'radio') return 'radio';
    if (t === 'select' || r === 'listbox' || r === 'combobox') return 'select';
    if (t === 'input' && ty === 'password') return 'password';
    if (t === 'textarea' || el.isContentEditable || r === 'textbox') return 'textarea';
    if (t === 'input' && /^(submit|button|reset|image)$/.test(ty)) return 'button';
    if (t === 'input' && /^(file|hidden)$/.test(ty)) return ty;
    if (t === 'input') return 'textfield';
    if (t === 'a' || r === 'link') return 'link';
    if (r === 'tab') return 'tab';
    if (r === 'option' || r === 'menuitem' || r === 'menuitemradio' || r === 'menuitemcheckbox') return 'option';
    return 'button';
  }
  function question(el){
    var g = el.closest('fieldset,[role=radiogroup],[role=group],[role=listitem],[data-params],.que,.question,.form-group');
    if (!g) return '';
    var h = g.querySelector('legend,[role=heading],h1,h2,h3,h4,.qtext,label');
    var q = txt(h ? h.innerText : '');
    return q;
  }
  var sel = 'button,a[href],input,textarea,select,[role=button],[role=link],[role=tab],[role=checkbox],[role=radio],' +
            '[role=switch],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox],[role=option],[role=textbox],' +
            '[role=combobox],[role=listbox],[contenteditable=true],[contenteditable=""]';
  var els = document.querySelectorAll(sel);
  for (var i = 0; i < els.length && n < MAX; i++) {
    var el = els[i], r = role(el);
    if (r === 'hidden') continue;
    // a styled checkbox hides its <input>; its <label> is what shows
    var box = (r === 'checkbox' || r === 'radio') && el.tagName === 'INPUT';
    if (!shown(el) && !(box && el.closest('label') && shown(el.closest('label')))) continue;
    var name = label(el);
    if (!name && !/^(textfield|textarea|password|select)$/.test(r)) continue;
    n++;
    el.setAttribute('data-dmx-id', String(n));
    var o = {id: n, role: r, name: name};
    if (r === 'checkbox' || r === 'radio')
      o.checked = (el.checked !== undefined && el.tagName === 'INPUT') ? !!el.checked : el.getAttribute('aria-checked') === 'true';
    if (r === 'textfield' || r === 'textarea') o.value = txt(el.value !== undefined && el.tagName !== 'DIV' ? el.value : el.innerText);
    if (r === 'select' && el.options) {
      o.value = txt(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '');
      o.options = [].slice.call(el.options, 0, 15).map(function(x){ return txt(x.text); });
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') o.disabled = true;
    if (el.required || el.getAttribute('aria-required') === 'true') o.required = true;
    var q = question(el); if (q && q !== name) o.group = q;
    if (r === 'button' && (el.type === 'submit' || /\b(submit|send|turn in|hand in|finish attempt|post|publish|place order|pay)\b/i.test(name))) o.submits = true;
    out.push(o);
  }
  var dlg = document.querySelector('[role=dialog]:not([aria-hidden=true]),[role=alertdialog],dialog[open]');
  return JSON.stringify({url: location.href, title: document.title, elements: out, truncated: n >= MAX,
                         dialog: dlg && shown(dlg) ? txt(dlg.innerText).slice(0, 200) : ''});
})()
"""

_JS_OFF = ("[-] Brave isn't letting me read pages. In Brave: View → Developer → Allow JavaScript from Apple Events "
           "(one time), then ask again.")


def observe() -> dict:
    """{"success", "url", "title", "elements": [...], "dialog"} for the front Brave tab."""
    from actuators.browser import browser_execute_js
    r = browser_execute_js(OBSERVE_JS)
    if not r.get("success"):
        msg = str(r.get("message", ""))
        if "Apple Events" in msg or "JavaScript" in msg or "not allowed" in msg.lower():
            return {"success": False, "message": _JS_OFF}
        return {"success": False, "message": msg or "[-] I couldn't read the page."}
    try:
        data = json.loads(r.get("value") or "{}")
    except ValueError:
        return {"success": False, "message": "[-] The page answered with something I couldn't read."}
    data["success"] = True
    return data
