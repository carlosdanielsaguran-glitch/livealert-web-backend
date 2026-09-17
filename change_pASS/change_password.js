document.addEventListener("DOMContentLoaded", async () => {
  // Same session check as dashboard.js's initSessionVerificationGuard —
  // no valid token, no business being on this page.
  const token = localStorage.getItem("livealert_token");
  const sessionExpirationTime = localStorage.getItem("livealert_session_expires_at");

  if (!token || !sessionExpirationTime || Date.now() >= parseInt(sessionExpirationTime, 10)) {
    window.location.href = "../auth/auth.html";
    return;
  }

  if (window.LiveAlertConfigPromise) {
    try {
      await window.LiveAlertConfigPromise;
    } catch (err) {
      console.warn("LiveAlert config discovery failed:", err);
    }
  }

  document.getElementById("set-password-btn")?.addEventListener("click", handleSetPassword);
  initPasswordToggles();
});

async function handleSetPassword() {
  const newPasswordInput = document.getElementById("new-password");
  const confirmPasswordInput = document.getElementById("confirm-password");
  const errorLabel = document.getElementById("change-error");
  const successLabel = document.getElementById("change-success");
  const submitBtn = document.getElementById("set-password-btn");

  if (errorLabel) errorLabel.textContent = "";
  if (successLabel) successLabel.textContent = "";

  const newPassword = newPasswordInput?.value || "";
  const confirmPassword = confirmPasswordInput?.value || "";

  if (newPassword.length < 8) {
    if (errorLabel) errorLabel.textContent = "Password must be at least 8 characters long.";
    return;
  }
  if (newPassword !== confirmPassword) {
    if (errorLabel) errorLabel.textContent = "Passwords do not match. Please verify.";
    return;
  }
  // The whole point of this page is leaving the shared default behind —
  // block re-submitting it as your "personal" password.
  if (newPassword === "default123") {
    if (errorLabel) errorLabel.textContent = "Choose a password other than the default one.";
    return;
  }

  const token = localStorage.getItem("livealert_token");
  if (!token) {
    window.location.href = "../auth/auth.html";
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "Saving...";

  try {
    // auth.py's /auth/change-password flow is what actually clears is_new
    // on the account doc (see accounts.js's comment on that field) — this
    // page doesn't need to touch that flag itself.
    await LiveAlertAPI.changePassword(token, newPassword);

    if (successLabel) successLabel.textContent = "Password set. Redirecting...";
    setTimeout(() => {
      window.location.href = "../dashboard/dashboard.html";
    }, 400);
  } catch (err) {
    if (errorLabel) errorLabel.textContent = err.message || "Could not set your password. Try again.";
    submitBtn.disabled = false;
    submitBtn.textContent = "Set Password & Continue";
  }
}

function initPasswordToggles() {
  const toggles = [
    { buttonId: "toggle-new-pass", inputId: "new-password" },
    { buttonId: "toggle-confirm-pass", inputId: "confirm-password" }
  ];

  toggles.forEach(({ buttonId, inputId }) => {
    const button = document.getElementById(buttonId);
    const input = document.getElementById(inputId);
    if (!button || !input) return;

    button.addEventListener("click", () => {
      const isPassword = input.getAttribute("type") === "password";
      input.setAttribute("type", isPassword ? "text" : "password");
      button.classList.toggle("fa-eye-slash", !isPassword);
      button.classList.toggle("fa-eye", isPassword);
    });
  });
}