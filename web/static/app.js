'use strict';
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
function syncTheme() {
  const dark = tg?.colorScheme ? tg.colorScheme === 'dark' : systemTheme.matches;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const background = dark ? '#17241e' : '#f7f7f2';
  document.querySelector('meta[name="theme-color"]').content = background;
  tg?.setHeaderColor?.(background);
  tg?.setBackgroundColor?.(background);
}
syncTheme();
tg?.onEvent?.('themeChanged', syncTheme);
systemTheme.addEventListener('change', syncTheme);
const token = tg?.initDataUnsafe?.start_param || new URLSearchParams(location.search).get('tgWebAppStartParam');
const app = document.getElementById('app');
const navigation = document.querySelector('nav');
const botUrl = document.querySelector('meta[name="kintab-bot-url"]').content;
let state, page = '', busy = false, toastTimer;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#icon-${name}"/></svg>`;
const categoryStyles = new Map([
  ['Groceries',['bag','sage']], ['Utilities',['bolt','sand']], ['Rent',['home','sage']],
  ['Transport',['car','blue']], ['Eating out',['cup','peach']],
]);
const categoryStyle = category => categoryStyles.get(category) || ['history','sage'];
const member = id => state.members.find(m => m.id === Number(id));
const wallet = id => state.wallets.find(w => w.id === Number(id));
const activeMembers = () => state.members.filter(m => !m.left_at);
const own = id => state.me.wallet_ids.includes(Number(id));
const todayMonth = () => new Date().toISOString().slice(0, 7);
function money(value, currency = false) {
  const d = state.group.minor_units;
  const n = Number(value);
  const sign = n < 0 ? '−' : '';
  const whole = Math.floor(Math.abs(n) / 10 ** d).toLocaleString('en');
  const fraction = d ? '.' + String(Math.abs(n) % 10 ** d).padStart(d, '0') : '';
  return `${currency ? esc(state.group.currency) + ' ' : ''}${sign}${whole}${fraction}`;
}
function rawAmount(value) {
  const d = state.group.minor_units;
  return (value / 10 ** d).toFixed(d);
}
function parseMoney(value) {
  const d = state.group.minor_units;
  const input = String(value).trim();
  if (!/^(?:\d+|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?$/.test(input)) throw Error('Enter an amount like 450 or 1,200.50.');
  const [whole, fraction = ''] = input.replaceAll(',', '').split('.');
  const result = Number(whole) * 10 ** d + Number(fraction.padEnd(d, '0'));
  if (fraction.length > d || !Number.isSafeInteger(result) || result <= 0 || result > 1e12) throw Error('Check the amount and decimal places.');
  return result;
}
function toast(message, error = false) {
  const box = document.getElementById('toast'); box.textContent = message; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => box.hidden = true, 5000);
  tg?.HapticFeedback?.notificationOccurred(error ? 'error' : 'success');
}
async function api(route, data) {
  if (!token || !tg?.initData) throw Error('Open this tab using the Open tab button in your Telegram group.');
  const response = await fetch(`/api/${encodeURIComponent(token)}/${route}`, {
    method: data ? 'POST' : 'GET', headers: {'X-Telegram-Init-Data': tg.initData, ...(data ? {'Content-Type':'application/json'} : {})},
    body: data ? JSON.stringify(data) : undefined,
  });
  const body = await response.json();
  if (!response.ok) throw Error(body.error || 'Could not load the tab.');
  return body;
}
async function refresh() {
  state = await api('state');
  document.getElementById('group-title').textContent = state.group.title;
  navigation.hidden = false;
  document.body.dataset.view = 'tab';
}
async function mutate(action, values = {}, target = null) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const result = await api('action', {action, ...values});
    if (result.forgotten || (action === 'leave' && (!values.member_id || values.member_id === state.me.id))) {
      app.innerHTML = `<div class="empty"><h1>${result.forgotten ? 'Tab deleted' : 'You left the tab'}</h1><p>${result.forgotten ? 'The group can start a fresh tab with /start.' : 'Your history and unsettled balance stay on the tab. Tap I’m in in the group to rejoin.'}</p></div>`;
      state = null; navigation.hidden = true; return;
    }
    await refresh(); toast('Saved');
    if (target) { location.hash = target; if (page === target) await renderPage(); }
    else await renderPage();
  } catch (error) { toast(error.message, true); }
  finally { busy = false; document.querySelectorAll('button').forEach(b => b.disabled = false); }
}
const button = (label, action, attrs = '', style = 'small') => `<button class="${style}" data-action="${action}" ${attrs}>${label}</button>`;
const empty = (title, detail = '') => `<div class="empty"><div class="symbol">${icon('check')}</div><h2>${title}</h2><p>${detail}</p></div>`;
const heading = (title, sub, eyebrow = 'THE HOUSEHOLD TAB') => `<div class="heading"><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="muted">${sub}</p></div>`;
function welcome(error = '') {
  navigation.hidden = true;
  document.body.dataset.view = 'welcome';
  app.innerHTML = `<section class="onboarding"><div class="onboarding-symbol">${icon('group')}</div>
    <h1>Your household tab.</h1><p class="muted">Shared expenses and simpler settlements, right in Telegram.</p>
    ${error ? `<div class="notice" role="alert">${esc(error)}</div>` : ''}
    <div class="onboarding-actions"><a class="button primary" data-telegram-link href="${esc(botUrl)}?startgroup=welcome">${icon('plus')} Add KinTab to your group</a>
    <a class="button" data-telegram-link href="${esc(botUrl)}">Open KinTab in Telegram ${icon('arrow')}</a></div>
    <ol class="onboarding-steps"><li><strong>Choose your group</strong><p>Telegram will ask where to add KinTab.</p></li>
    <li><strong>Tap “I’m in”</strong><p>Join your household’s tab from the bot’s welcome message.</p></li>
    <li><strong>Open your tab</strong><p>Tap “Open tab” in the group to see your shared expenses.</p></li></ol>
    <p class="form-note">Already using KinTab? Open your Telegram group and tap <strong>Open tab</strong>. Send <strong>/tab</strong> for a fresh button.</p></section>`;
}
function walletLines(lines, controls = false) {
  if (!lines.length) return empty('All settled.', 'A little less to keep track of. Add an expense when someone pays for the household.');
  return lines.map(line => {
    const source = wallet(line.from_wallet_id), target = wallet(line.to_wallet_id);
    return `<div class="wallet-line"><div class="row"><span class="avatar">${esc(source.name.slice(0,1))}</span><div class="wallet-person"><div class="wallet-route"><strong>${esc(source.name)}</strong> ${icon('arrow')} <span>${esc(target.name)}</span></div>
      <div class="sub">${esc(source.settler?.name || source.name)} settles</div></div><span class="amount">${money(line.amount_minor)}</span></div>
      ${controls ? `<div class="actions">${own(source.id) || own(target.id) ? `<a class="button small" href="#transfer:payment:${source.id}:${target.id}:${line.amount_minor}">Record payment</a>` : ''}
      ${own(target.id) ? `<a class="button small" href="#transfer:writeoff:${source.id}:${target.id}:${line.amount_minor}">Forgive</a>` : ''}</div>` : ''}</div>`;
  }).join('');
}
function entryCard(e, entity, compact = false) {
  const expense = entity === 'expense';
  const inactive = expense ? e.deleted_at : e.undone_at;
  const title = expense ? e.description : {payment:'Payment', opening:'Opening balance', writeoff:'Debt forgiven'}[e.kind];
  const detail = expense ? `${member(e.payer_member_id)?.name} paid · ${e.category}` :
    `${wallet(e.from_wallet_id)?.name} → ${wallet(e.to_wallet_id)?.name}${e.kind === 'opening' ? ' · credit → debt' : ''}`;
  const [entryIcon, tint] = expense ? categoryStyle(e.category) : ['transfer','blue'];
  return `<article class="entry ${inactive ? 'inactive' : ''} ${compact ? 'compact' : ''}"><div class="row entry-main"><span class="entry-icon ${tint}">${icon(entryIcon)}</span><div class="entry-copy"><div class="entry-title">${esc(title)}</div>
    <div class="sub">${esc(detail)}</div><time datetime="${esc(e.created_at)}">${new Date(e.created_at).toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'})}</time></div>
    <div class="amount">${money(e.amount_minor)}<div class="sub">${inactive ? (expense ? 'Deleted' : 'Undone') : esc(state.group.currency)}</div></div></div>
    ${compact ? '' : `${expense ? `<details><summary>Who shares this?</summary>${e.shares.map(s => `<div class="row sub"><span>${esc(member(s.member_id)?.name)} · ${esc(wallet(s.wallet_id)?.name)}</span><span>${money(s.amount_minor)}</span></div>`).join('')}</details>` : ''}
    <div class="actions">${expense && e.can_edit ? `<a class="button small" href="#expense:${e.id}">Edit</a>${button('Delete','delete-expense',`data-id="${e.id}"`,'small danger')}` : ''}
    ${expense && e.can_review ? button('Not mine?','review',`data-id="${e.id}"`) : ''}
    ${!expense && e.can_undo ? button('Undo','undo',`data-id="${e.id}"`) + (e.kind === 'opening' ? `<a class="button small" href="#opening:${e.id}">Edit</a>` : '') : ''}</div>`}</article>`;
}
function historyItems(expenses, transfers, limit = Infinity, compact = false) {
  const all = [...expenses.map(e => ({e,entity:'expense'})), ...transfers.map(e => ({e,entity:'transfer'}))];
  return all.sort((a,b) => b.e.created_at.localeCompare(a.e.created_at)).slice(0,limit).map(({e,entity}) => entryCard(e,entity,compact)).join('') || empty('No entries yet.', 'Your household’s expenses and payments will appear here.');
}
function home() {
  const ownBalance = state.me.wallet_ids.reduce((n,id) => n + (state.balances[id] || 0), 0);
  app.innerHTML = heading('A little more in balance.', `Your shared spending, all in one place.`) +
    `<section class="card balance-card home-balance"><img class="balance-art" src="static/images/balance-sculpture.png" alt="" aria-hidden="true"><div class="balance-content"><div class="eyebrow">${icon('wallet')} ${esc(wallet(state.me.wallet_id)?.name)} · ${ownBalance === 0 ? 'ALL SETTLED' : ownBalance > 0 ? 'YOU ARE OWED' : 'YOU OWE'}</div>
    <div class="amount"><span class="currency">${esc(state.group.currency)}</span> ${money(Math.abs(ownBalance))}</div>
    <div class="sub">${ownBalance === 0 ? 'Your wallet is all settled.' : 'Across your wallet and its history.'}</div>
    <a class="balance-link" href="#breakdown">Why this amount? ${icon('arrow')}</a></div></section>
    <div class="quick-actions"><a href="#expense" class="button primary" aria-label="＋ Add expense">${icon('plus')} Add expense</a><a href="#settle" class="button">${icon('transfer')} Settle up</a></div>
    <div class="overview-columns"><div>
    <div class="section-heading"><h2>Who owes whom</h2><span class="tag">Simplified · ${esc(state.group.currency)}</span></div>
    <section class="card settlement-card">${walletLines(state.simplified)}</section></div><div>
    <div class="section-heading"><h2>Recent activity</h2><a class="ghost" href="#history">View all ↗</a></div>
    <section class="card activity-card">${historyItems(state.expenses,state.transfers,4,true)}</section></div></div>
    <div class="page-footer">${icon('check')} Shared, settled. A little less to keep track of.</div>`;
}
async function history() {
  app.innerHTML = heading('The whole picture.', 'Every expense, payment and change. Nothing to keep in your head.', 'HISTORY') +
    `<form id="filters" class="filters"><label>Month<input name="month" type="month"></label>
    <label>Category<select name="category"><option value="">All categories</option>${[...new Set(state.expenses.map(e=>e.category))].map(c=>`<option>${esc(c)}</option>`).join('')}</select></label>
    <label>Payer<select name="payer"><option value="">All payers</option>${state.members.map(m=>`<option value="${m.id}">${esc(m.name)}</option>`).join('')}</select></label></form>
    <div class="row"><span class="sub">${esc(state.group.currency)} · UTC dates</span>${button('Download CSV','export')}</div><section id="history-list" class="card" style="margin-top:16px">${historyItems(state.expenses,state.transfers)}</section>
    <details class="card"><summary>Audit trail · latest 200 changes</summary>${state.audit.map(row=>`<div class="audit-item"><div class="row"><strong>${esc(row.action)} · ${esc(row.entity)} ${row.entity_id ?? ''}</strong><span class="sub">${esc(member(row.member_id)?.name || 'System')}</span></div><time class="sub">${esc(row.created_at)}</time><details><summary>Details</summary><pre>${esc(row.before_json || '—')}\n→\n${esc(row.after_json || '—')}</pre></details></div>`).join('')}</details>`;
  document.getElementById('filters').addEventListener('change', async event => {
    try {
      const data = await api('history?' + new URLSearchParams(new FormData(event.currentTarget)));
      document.getElementById('history-list').innerHTML = historyItems(data.expenses, data.transfers);
    } catch(error) { toast(error.message, true); }
  });
}
function expenseForm(id) {
  const e = id ? state.expenses.find(e => e.id === Number(id)) : null;
  if (id && (!e || !e.can_edit)) { app.innerHTML = empty('This expense is read-only.'); return; }
  const spec = e ? JSON.parse(e.split_spec) : null;
  const savedIds = new Set(spec?.members.map(m => m.id) || []);
  const savedWalletIds = new Set(spec?.members.map(m => m.wallet_id) || []);
  app.innerHTML = heading(e ? 'Update the expense.' : 'Someone picked up the bill.', 'Shares are calculated in exact minor units.', e ? 'EDIT EXPENSE' : 'ADD EXPENSE') +
    `<form id="expense-form" class="card"><div class="grid"><label class="field">Amount · ${esc(state.group.currency)}<input name="amount" inputmode="decimal" placeholder="0.00" required value="${e ? rawAmount(e.amount_minor) : ''}"></label>
    <label class="field">Who paid?<select name="payer">${state.members.filter(m=>!m.left_at || m.id===e?.payer_member_id).map(m=>`<option value="${m.id}" ${m.id===(e?.payer_member_id || state.me.id)?'selected':''}>${esc(m.name)}${m.tg_user_id ? '' : ' (guest)'}</option>`).join('')}</select></label></div>
    <label class="field">Description<input name="description" maxlength="200" placeholder="e.g. The weekly groceries" required value="${esc(e?.description || '')}"></label>
    <label class="field">Category<input name="category" maxlength="60" list="categories" value="${esc(e?.category || 'Other')}"><datalist id="categories">${['Groceries','Utilities','Rent','Transport','Eating out','Other'].map(c=>`<option>${c}</option>`).join('')}</datalist></label>
    ${e ? `<div class="field"><strong>Current shares</strong>${e.shares.map(s => `<div class="row sub"><span>${esc(member(s.member_id)?.name || 'Unknown person')} · ${esc(wallet(s.wallet_id)?.name || 'Unknown wallet')}</span><span>${money(s.amount_minor)}</span></div>`).join('')}</div><label class="check"><input id="change-split" type="checkbox" checked>Change who shares this expense</label><p class="form-note">The saved split, weights and wallets are preserved unless you change them. Changing the split uses current members and wallets.</p>` : ''}
    <div id="split-fields"><label class="field">Split between<select id="split-selection"><option value="all">Everyone · group default (${esc(state.group.split_mode)})</option><option value="person">Pick people</option><option value="wallet">Pick wallets equally</option><option value="custom">People with custom weights</option></select></label><div id="participants"></div></div>
    <p class="form-note">The payer covers their own share. Any spare minor units go to the payer if included, otherwise to people in join order.</p>
    <div class="actions"><button class="primary" type="submit">${e?'Save changes':'Add expense'}</button><a class="button" href="#${e?'history':'home'}">Cancel</a></div></form>`;
  let splitChanged = false;
  document.getElementById('change-split')?.addEventListener('change', event => {
    document.getElementById('split-fields').hidden = !event.target.checked;
    splitChanged = event.target.checked;
  });
  document.getElementById('split-fields').addEventListener('change', () => { splitChanged = true; });
  document.getElementById('split-fields').addEventListener('input', () => { splitChanged = true; });
  const selection = document.getElementById('split-selection');
  const renderParticipants = (saved = false) => {
    const mode = selection.value;
    document.getElementById('participants').innerHTML = mode === 'all' ? '' : mode === 'wallet' ?
      state.wallets.filter(w=>activeMembers().some(m=>m.wallet_id===w.id)).map(w=>`<label class="check"><input name="participant" type="checkbox" value="${w.id}" ${!saved || savedWalletIds.has(w.id) ? 'checked' : ''}><span>${esc(w.name)}</span></label>`).join('') :
      activeMembers().map(m=>`<label class="check"><input name="participant" type="checkbox" value="${m.id}" ${!saved || savedIds.has(m.id) ? 'checked' : ''}><span>${esc(m.name)}</span>${mode==='custom'?`<input aria-label="Weight for ${esc(m.name)}" class="weight" name="weight-${m.id}" type="number" step="any" min="0.001" max="1000" value="${esc(saved ? (spec.weights?.[m.id] ?? spec.members.find(person=>person.id===m.id)?.weight ?? m.weight) : m.weight)}">`:''}</label>`).join('');
  };
  selection.addEventListener('change', () => renderParticipants());
  if (e) {
    selection.value = spec.weights ? 'custom' : spec.mode === 'wallet' ? 'wallet' : 'person';
    renderParticipants(true);
  }
  document.getElementById('expense-form').addEventListener('submit', event => {
    event.preventDefault();
    try {
      const data = new FormData(event.currentTarget);
      const values = {amount_minor:parseMoney(data.get('amount')),payer_member_id:Number(data.get('payer')),description:data.get('description'),category:data.get('category') || 'Other'};
      if (!e || (document.getElementById('change-split').checked && splitChanged)) {
        const mode = selection.value;
        if (mode !== 'all') {
          const ids = data.getAll('participant').map(Number);
          if (!ids.length) throw Error('Choose at least one participant.');
          values[mode === 'wallet' ? 'wallet_ids' : 'member_ids'] = ids;
          values.split_mode = mode === 'wallet' ? 'wallet' : 'person';
          if (mode === 'custom') values.weights = Object.fromEntries(ids.map(id=>[id,data.get(`weight-${id}`)]));
        } else if (e) { values.member_ids = activeMembers().map(m=>m.id); values.split_mode = state.group.split_mode; }
      }
      if (e) Object.assign(values,{id:e.id,revision:e.revision});
      mutate(e?'edit_expense':'add_expense',values,'history');
    } catch(error) { toast(error.message,true); }
  });
}
function settle() {
  app.innerHTML = heading('Bring things even.', 'Send money your usual way, then record it here.', 'SETTLE UP') +
    `<section class="card">${walletLines(state.lines,true)}</section><div class="actions"><a class="button primary" href="#transfer:payment">Record a payment</a><a class="button" href="#transfer:opening">Add opening balance</a></div>
    <p class="form-note">Only the wallet that’s owed can forgive a debt. Opening balances record existing debt; they aren’t household spending.</p>`;
}
function transferForm(kind='payment', from='', to='', amount='') {
  if (!['payment','opening','writeoff'].includes(kind)) kind='payment';
  const wallets = state.wallets.filter(w=>!w.closed_at || state.balances[w.id] || state.lines.some(l=>l.from_wallet_id===w.id||l.to_wallet_id===w.id));
  from ||= kind==='opening' ? state.me.wallet_id : kind==='payment' ? state.me.wallet_id : '';
  to ||= kind==='writeoff' ? state.me.wallet_id : '';
  const options = selected => wallets.map(w=>`<option value="${w.id}" ${w.id===Number(selected)?'selected':''}>${esc(w.name)}</option>`).join('');
  const labels = {payment:['Record a payment.','Wallet that sent money','Wallet that received money'],opening:['Start with what’s owed.','Wallet that is owed (creditor)','Wallet that owes (debtor)'],writeoff:['Let this one go.','Wallet that owes (debtor)','Your wallet (creditor)']}[kind];
  app.innerHTML = heading(labels[0], kind==='writeoff'?'This reduces the amount owed and leaves an audit record.':'This records a balance change between two wallets.',kind.toUpperCase()) +
    `<form id="transfer-form" class="card"><label class="field">${labels[1]}<select name="from">${options(from)}</select></label><label class="field">${labels[2]}<select name="to">${options(to)}</select></label><label class="field">Amount · ${esc(state.group.currency)}<input name="amount" inputmode="decimal" required value="${amount?rawAmount(Number(amount)):''}" placeholder="0.00"></label><button class="primary" type="submit">${kind==='writeoff'?'Forgive debt':'Save '+kind}</button></form>`;
  document.getElementById('transfer-form').addEventListener('submit',event=>{
    event.preventDefault(); try { const data=new FormData(event.currentTarget); mutate('add_transfer',{kind,from_wallet_id:Number(data.get('from')),to_wallet_id:Number(data.get('to')),amount_minor:parseMoney(data.get('amount'))},'settle'); } catch(error){toast(error.message,true);}
  });
}
function openingForm(id) {
  const entry=state.transfers.find(t=>t.id===Number(id));
  if (!entry || entry.kind!=='opening' || !entry.can_undo) { app.innerHTML=empty('This opening balance is read-only.');return; }
  app.innerHTML=heading('Update the starting point.','The two wallets stay the same.','OPENING BALANCE')+`<form id="opening-form" class="card"><label class="field">Amount · ${esc(state.group.currency)}<input inputmode="decimal" name="amount" value="${rawAmount(entry.amount_minor)}" required></label><button class="primary">Save changes</button></form>`;
  document.getElementById('opening-form').addEventListener('submit',event=>{event.preventDefault();try{mutate('edit_opening',{id:entry.id,revision:entry.revision,amount_minor:parseMoney(new FormData(event.currentTarget).get('amount'))},'history');}catch(error){toast(error.message,true);}});
}
async function breakdown(id=state.me.wallet_id) {
  const data=await api('breakdown?wallet='+Number(id));
  app.innerHTML=heading('Every part of the balance.','Money paid − your share + transfers = your net balance.','WHY THIS AMOUNT?')+
    `<label class="field">Wallet<select id="breakdown-wallet">${state.wallets.map(w=>`<option value="${w.id}" ${w.id===Number(id)?'selected':''}>${esc(w.name)}</option>`).join('')}</select></label>
    <div class="card"><div class="row"><h2>${esc(wallet(id)?.name)}</h2><strong>${money(data.balance_minor,true)}</strong></div>
    ${data.rows.map(row=>`<div class="entry"><div class="row"><div><strong>${esc(row.description)}</strong><div class="sub">${row.created_at.slice(0,10)}${row.entity==='expense'?` · paid ${money(row.paid_minor)} − share ${money(row.share_minor)}`:''}</div></div><strong class="${row.delta_minor>=0?'positive':'negative'}">${row.delta_minor>0?'+':''}${money(row.delta_minor)}</strong></div></div>`).join('')||empty('No balance changes yet.')}</div>`;
  document.getElementById('breakdown-wallet').addEventListener('change',event=>{location.hash='breakdown:'+event.target.value;});
}
async function monthly(month=todayMonth()) {
  const data=await api('summary?month='+encodeURIComponent(month));
  app.innerHTML=heading('A month, accounted for.','Household spending and the shares behind it.','MONTHLY SUMMARY')+
    `<label class="field">Month<input id="summary-month" type="month" value="${esc(month)}"></label><div class="card balance-card"><div class="eyebrow">TOTAL HOUSEHOLD SPEND</div><div class="amount"><span class="currency">${esc(state.group.currency)}</span> ${money(data.total_minor)}</div><div class="sub">Expenses only · payments and opening balances excluded</div></div>
    <div class="section-heading"><h2>Each wallet’s share</h2><span class="tag">${esc(month)}</span></div><div class="card">${Object.entries(data.wallet_shares).map(([id,share])=>`<div class="entry"><div class="row"><strong>${esc(wallet(id)?.name)}</strong><span>${money(share)}</span></div><div class="bar"><span style="width:${data.total_minor?share/data.total_minor*100:0}%"></span></div></div>`).join('')||empty('A fresh page.','No expenses were logged this month.')}</div>`;
  document.getElementById('summary-month').addEventListener('change',event=>{if(event.target.value)location.hash='summary:'+event.target.value;});
}
function groupWallet(w, active) {
  const people = active.filter(m=>m.wallet_id===w.id);
  return `<section class="card" data-wallet-id="${w.id}"><div class="row"><h3>${esc(w.name)}</h3><strong>${money(w.balance_minor)}</strong></div>
    ${state.members.filter(m=>m.wallet_id===w.id).map(m=>`<div class="row wrap wallet-line"><div class="row start"><span class="avatar">${esc(m.name.slice(0,1))}</span><span>${esc(m.name)} <span class="sub">${m.left_at?'· left':m.tg_user_id?'':'· guest'}</span></span></div>
    <div class="row wrap">${m.id===w.settler_member_id?'<span class="tag">Settler</span>':''}
    ${!m.left_at&&people.length>1?button('Separate wallet','unlink-member',`data-id="${m.id}" aria-label="Separate wallet for ${esc(m.name)}"`):''}
    ${!m.left_at&&m.id!==state.me.id?button('Remove','remove',`data-id="${m.id}"`,'ghost danger'):''}</div></div>`).join('')}
    ${people.length?`<form class="settler-form row" style="margin-top:18px"><label class="field" style="flex:1;margin:0">Settler<select name="member" aria-label="Settler for ${esc(w.name)}">${people.map(m=>`<option value="${m.id}" ${m.id===w.settler_member_id?'selected':''}>${esc(m.name)}</option>`).join('')}</select></label><button class="small">Save</button></form>`:''}</section>`;
}
function walletSetupForm(active) {
  const wallets = state.wallets.filter(w=>!w.closed_at&&w.balance_minor===0&&active.some(m=>m.wallet_id===w.id));
  return `<section class="card"><h3>Manage shared wallets</h3>
    <p class="sub">Combine wallets directly, including guests, without waiting for invitations. Everyone active in the first wallet moves to the second; the second wallet’s settler stays in charge.</p>
    <p class="form-note">Both wallets must have zero balance. Past entries keep their original wallets. Use Separate wallet above to give one person their own wallet.</p>
    ${wallets.length>1?`<form id="link-wallets-form"><div class="grid"><label class="field">Wallet to move<select name="from_wallet_id">${wallets.map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('')}</select></label>
    <label class="field">Combine into<select name="to_wallet_id">${wallets.map((w,i)=>`<option value="${w.id}" ${i===1?'selected':''}>${esc(w.name)}</option>`).join('')}</select></label></div><button class="primary">Combine wallets</button></form>`:'<p class="sub">You need two separate wallets with zero balance to combine them.</p>'}</section>`;
}
function memberSetupForm(active) {
  const wallets=state.wallets.filter(w=>!w.closed_at&&active.some(m=>m.wallet_id===w.id));
  const options=wallets.map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('');
  return `<section class="card"><h3>Add a member</h3><p class="sub">Add someone by name before they use the bot. Give them their own wallet or put them straight into a shared wallet.</p>
    <form id="member-form"><div class="grid"><label class="field">Member name<input name="name" placeholder="Their name" maxlength="100" required></label><label class="field">Starting wallet<select name="wallet_id" aria-label="Starting wallet"><option value="">Create a new wallet</option>${options}</select></label></div><button class="primary">Add member</button></form></section>
    ${active.length?`<section class="card"><h3>Assign a wallet</h3><p class="sub">Move one person into a shared wallet or give them a new wallet of their own.</p>
    <form id="assign-wallet-form"><div class="grid"><label class="field">Member<select name="member_id" aria-label="Member" required><option value="">Choose a person…</option>${active.map(m=>`<option value="${m.id}">${esc(m.name)}</option>`).join('')}</select></label><label class="field">Wallet<select name="wallet_id" aria-label="Wallet"><option value="">Create their own wallet</option>${options}</select></label></div><p class="form-note">Moving an existing member requires the old and new wallets to have zero balance. Past entries stay in their original wallets.</p><button class="small">Save wallet</button></form></section>`:''}`;
}
function groupPage() {
  const active=activeMembers();
  app.innerHTML=heading('A household, together.', 'People share expenses. Wallets settle between them.','GROUP')+
    state.invites.filter(i=>i.to_member_id===state.me.id).map(i=>`<div class="notice"><strong>${esc(member(i.from_member_id)?.name)} wants to share a wallet.</strong><p class="sub">Both wallets must be at zero. The proposer becomes the settler.</p><div class="actions">${button('Accept','accept-invite',`data-id="${i.id}"`,'small primary')}${button('Decline','decline-invite',`data-id="${i.id}"`)}</div></div>`).join('')+
    memberSetupForm(active)+
    `<div class="section-heading"><h2>Wallets & members</h2><span class="tag">${active.length} people</span></div>`+
    state.wallets.filter(w=>!w.closed_at || state.balances[w.id] || state.lines.some(l=>l.from_wallet_id===w.id||l.to_wallet_id===w.id)).map(w=>groupWallet(w,active)).join('')+
    walletSetupForm(active)+
    `<section class="card"><h3>Share a wallet</h3><p class="sub">Send an invitation to share your wallet. Both wallets must be settled; the invited person accepts.</p><form id="invite-form" class="row"><select aria-label="Invite a member" name="member"><option value="">Choose a person…</option>${active.filter(m=>m.wallet_id!==state.me.wallet_id&&m.tg_user_id).map(m=>`<option value="${m.id}">${esc(m.name)}</option>`).join('')}</select><button class="small">Invite</button></form><div class="actions">${button('Leave shared wallet','unlink')}
    </div>${state.invites.filter(i=>i.from_member_id===state.me.id).map(i=>`<p class="sub">Waiting for ${esc(member(i.to_member_id)?.name)} to respond.</p>`).join('')}</section>
    ${settingsForm()}
    <section class="card"><h3>Leave the tab</h3><p class="sub">You stop sharing new expenses. Your past entries and unsettled balance remain.</p>${button('Leave tab','leave','','small danger')}<hr><h3>Delete the whole tab</h3><p class="sub">Permanently removes every member, entry and audit record for this group.</p>${button('Delete tab…','forget','','small danger')}</section>`;
  document.getElementById('invite-form').addEventListener('submit',event=>{event.preventDefault();const id=Number(new FormData(event.currentTarget).get('member'));if(id)mutate('invite',{member_id:id});});
  document.getElementById('member-form').addEventListener('submit',event=>{
    event.preventDefault();const data=new FormData(event.currentTarget);
    mutate('member',{name:data.get('name'),wallet_id:data.get('wallet_id')?Number(data.get('wallet_id')):null});
  });
  const assignment=document.getElementById('assign-wallet-form');
  assignment?.querySelector('[name="member_id"]').addEventListener('change',event=>{
    assignment.elements.wallet_id.value=member(event.target.value)?.wallet_id || '';
  });
  assignment?.addEventListener('submit',event=>{
    event.preventDefault();const data=new FormData(event.currentTarget);
    mutate('assign_wallet',{member_id:Number(data.get('member_id')),wallet_id:data.get('wallet_id')?Number(data.get('wallet_id')):null});
  });
  document.querySelectorAll('.settler-form').forEach(form=>form.addEventListener('submit',event=>{event.preventDefault();mutate('settler',{member_id:Number(new FormData(event.currentTarget).get('member'))});}));
  document.getElementById('link-wallets-form')?.addEventListener('submit',event=>{
    event.preventDefault();
    const data=new FormData(event.currentTarget),from=Number(data.get('from_wallet_id')),to=Number(data.get('to_wallet_id'));
    if(from===to){toast('Choose two different wallets.',true);return;}
    mutate('link_wallets',{from_wallet_id:from,to_wallet_id:to});
  });
  document.getElementById('settings-form')?.addEventListener('submit',event=>{event.preventDefault();const d=new FormData(event.currentTarget);mutate('settings',{currency:d.get('currency'),minor_units:Number(d.get('minor_units')),split_mode:d.get('split_mode'),debt_mode:d.get('debt_mode'),weights:Object.fromEntries(active.map(m=>[m.id,d.get('weight-'+m.id)]))});});
}
function settingsForm(){
  const g=state.group;
  return `<form id="settings-form" class="card"><h3>Group settings</h3><div class="grid"><label class="field">Currency code<input name="currency" pattern="[A-Z]{3}" maxlength="3" value="${esc(g.currency)}" required></label><label class="field">Decimal places<select name="minor_units" aria-label="Decimal places">${[0,1,2,3].map(d=>`<option ${d===g.minor_units?'selected':''}>${d}</option>`).join('')}</select></label></div><p class="form-note">Currency and decimal places are locked once entries exist.</p><div class="grid"><label class="field">Default split<select name="split_mode" aria-label="Default split"><option value="person" ${g.split_mode==='person'?'selected':''}>By person</option><option value="wallet" ${g.split_mode==='wallet'?'selected':''}>By wallet</option></select></label><label class="field">Pinned debts<select name="debt_mode" aria-label="Pinned debts"><option value="simplified" ${g.debt_mode==='simplified'?'selected':''}>Simplified</option><option value="direct" ${g.debt_mode==='direct'?'selected':''}>Direct</option></select></label></div><h3>Person weights</h3>${activeMembers().map(m=>`<label class="check"><span>${esc(m.name)}</span><input class="weight" name="weight-${m.id}" aria-label="Weight for ${esc(m.name)}" type="number" min="0.001" max="1000" step="any" value="${esc(m.weight)}" required></label>`).join('')}<p class="form-note">Weights apply to future expenses. A child can count as 0.5.</p><button class="primary">Save settings</button></form>`;
}
async function renderPage(){
  if(!state)return;
  const [route,...args]=(location.hash.slice(1)||'home').split(':');page=route;
  const activeRoute = ['settle','transfer','opening','breakdown'].includes(route) ? 'home' : route;
  document.querySelectorAll('nav a').forEach(a=>{
    const active = a.dataset.page === activeRoute;
    a.classList.toggle('active',active);
    if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
  });
  try{
    if(route==='home')home();else if(route==='history')await history();else if(route==='expense')expenseForm(...args);
    else if(route==='settle')settle();else if(route==='transfer')transferForm(...args);else if(route==='opening')openingForm(...args);
    else if(route==='breakdown')await breakdown(...args);else if(route==='summary')await monthly(...args);else if(route==='group')groupPage();else home();
  }catch(error){toast(error.message,true);}
}
app.addEventListener('click',async event=>{
  const link=event.target.closest('a[data-telegram-link]');
  if(link && tg?.initData && typeof tg.openTelegramLink === 'function'){
    try{tg.openTelegramLink(link.href);event.preventDefault();}catch{/* Keep the HTTPS link as a fallback. */}
    return;
  }
  const b=event.target.closest('[data-action]');if(!b||busy)return;
  const id=Number(b.dataset.id),action=b.dataset.action;
  if(action==='delete-expense')mutate('delete_expense',{id});
  else if(action==='review')mutate('review_expense',{id});
  else if(action==='undo')mutate('undo_transfer',{id});
  else if(action==='remove')mutate('leave',{member_id:id});
  else if(action==='leave')mutate('leave');
  else if(action==='unlink')mutate('unlink');
  else if(action==='unlink-member')mutate('unlink',{member_id:id});
  else if(action==='accept-invite'||action==='decline-invite')mutate('respond_invite',{id,accept:action==='accept-invite'});
  else if(action==='forget'){
    const confirmed=window.confirm('Permanently delete this group’s entire tab, including all entries and audit history?');
    if(confirmed)mutate('forget',{confirmation:state.group.token});
  }else if(action==='export'){
    try{
      const r=await fetch(`/api/${encodeURIComponent(token)}/export`,{headers:{'X-Telegram-Init-Data':tg.initData}});
      if(!r.ok)throw Error((await r.json()).error);
      const url=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=url;a.download='kintab.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
    }catch(error){toast(error.message,true);}
  }
});
window.addEventListener('hashchange',()=>{tg?.HapticFeedback?.selectionChanged();renderPage();window.scrollTo(0,0);});
async function boot(){
  if(!token || !tg?.initData){welcome();return;}
  try{await refresh();await renderPage();}catch(error){welcome(error.message);}
}
boot();
