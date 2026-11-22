"use strict";
(function () {
  const form = document.getElementById("login-form");
  const user = document.getElementById("username");
  const pass = document.getElementById("password");
  const err = document.getElementById("error");
  const submit = document.getElementById("submit");
  const toggle = document.getElementById("toggle");

  toggle.addEventListener("click", () => {
    const show = pass.type === "password";
    pass.type = show ? "text" : "password";
    toggle.textContent = show ? "Hide" : "Show";
    toggle.setAttribute("aria-pressed", String(show));
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (!user.value.trim() || !pass.value) {
      err.textContent = "Enter your username and password.";
      (user.value.trim() ? pass : user).focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = "Signing in…";
    try {
      const res = await fetch("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user.value.trim(), password: pass.value }),
      });
      if (res.ok) { location.href = "/"; return; }
      let msg = "Sign-in failed. Please try again.";
      try { msg = (await res.json()).error || msg; } catch (_) { /* keep default */ }
      err.textContent = msg;
      pass.value = "";
      pass.focus();
    } catch (_) {
      err.textContent = "Couldn't reach the server. Is it still running?";
    } finally {
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  });
})();
