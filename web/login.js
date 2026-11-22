"use strict";
(async function () {
  const grid = document.getElementById("users");
  const err = document.getElementById("error");
  try {
    const { users } = await (await fetch("/api/users")).json();
    grid.textContent = "";
    for (const u of users) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "user-card";
      const name = document.createElement("strong"); name.textContent = u.name;
      const meta = document.createElement("span"); meta.textContent = u.role + " · " + u.department;
      b.append(name, meta);
      b.addEventListener("click", async () => {
        err.textContent = "";
        const res = await fetch("/api/login", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: u.id }),
        });
        if (res.ok) location.href = "/";
        else err.textContent = "Sign-in failed.";
      });
      grid.appendChild(b);
    }
  } catch (e) {
    grid.textContent = "";
    err.textContent = "Couldn't load users. Is the server running?";
  }
})();
