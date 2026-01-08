'use strict';
const form = document.getElementById('login-form');
const message = document.getElementById('login-result');
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  message.textContent = '正在验证 / Signing in…';
  try {
    const response = await fetch('/auth/login', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CT-Request': '1' },
      body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value }) });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Login failed');
    form.elements.password.value = '';
    window.location.assign('index.html');
  } catch (error) {
    message.textContent = error.message;
  } finally { button.disabled = false; }
});
