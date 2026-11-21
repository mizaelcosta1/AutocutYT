(() => {
  const byId = id => document.getElementById(id);
  const form = byId('access-form'), email = byId('email'), password = byId('password'), code = byId('code');
  const feedback = byId('feedback'), submit = byId('submit-access');
  const titles = {signin: 'Entrar no estúdio', register: 'Criar sua conta', confirm: 'Confira seu e-mail'};
  const actions = {signin: 'Entrar', register: 'Enviar código', confirm: 'Confirmar e entrar'};
  let mode, busy = false;
  function show(next) {
    mode = next;
    const confirming = next === 'confirm';
    byId('form-title').textContent = titles[next];
    byId('form-help').textContent = {signin: 'Acesse seus vídeos, cortes e exportações.', register: 'Escolha uma senha de pelo menos 12 caracteres.', confirm: 'Digite os seis números enviados para ' + email.value + '. O código vale por 10 minutos.'}[next];
    email.readOnly = confirming;
    password.disabled = byId('password-row').hidden = confirming;
    password.minLength = next === 'register' ? 12 : 1;
    password.autocomplete = next === 'signin' ? 'current-password' : 'new-password';
    code.disabled = byId('code-row').hidden = !confirming;
    code.required = confirming;
    submit.textContent = actions[next];
    byId('switch-access').textContent = next === 'signin' ? 'Criar uma conta' : 'Voltar para entrar';
    byId('resend').hidden = !confirming;
    feedback.textContent = '';
    if (confirming) code.focus();
  }
  async function send(action) {
    if (busy) return;
    busy = true;
    form.setAttribute('aria-busy','true');
    for (const id of ['submit-access','switch-access','resend']) byId(id).disabled = true;
    feedback.textContent = 'Aguarde…';
    try {
      const payload = {email: email.value.trim(), ...(action === 'confirm' ? {code: code.value} : {password: password.value})};
      const res = await fetch('/login/email/' + action, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Confira os campos e tente novamente.');
      if (action === 'register') { code.value = ''; show('confirm'); feedback.textContent = data.message; }
      else { password.value = ''; location.assign(document.querySelector('main').dataset.next || '/'); }
    } catch (err) { feedback.textContent = err.message || 'Falha de conexão. Tente novamente.'; }
    finally {
      busy = false;
      form.removeAttribute('aria-busy');
      for (const id of ['submit-access','switch-access','resend']) byId(id).disabled = false;
    }
  }
  form.addEventListener('submit', ev => { ev.preventDefault(); send(mode); });
  byId('switch-access').onclick = () => { password.value = ''; show(mode === 'signin' ? 'register' : 'signin'); email.focus(); };
  byId('resend').onclick = () => send('register');
  show('signin');
})();
