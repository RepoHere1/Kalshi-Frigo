/**
 * model-cost — TUI modal picker. Command: /model-cost. Footnote updates on cursor.
 */
const WIDTH = 72
const PRICE_1K = {
  'openrouter/free': { in: 0, out: 0 },
  'deepseek/deepseek-chat': { in: 0.14, out: 0.28 },
  'deepseek/deepseek-reasoner': { in: 0.55, out: 2.19 },
  'anthropic/claude-3.5-sonnet': { in: 3.0, out: 15.0 },
  'google/gemini-2.5-pro': { in: 1.25, out: 10.0 },
  'meta-llama/llama-3.3-70b-instruct': { in: 0.9, out: 0.9 },
}
export default function register(sdk) {
  const { Dialog, React, Text, defineWidgetApp, h, updateWidget } = sdk
  const usd = v => typeof v === 'number' && isFinite(v) ? (v < 0.00005 ? '$0.0000' : `$${v.toFixed(4)}`) : '—'
  const kfmt = n => { if (typeof n !== 'number' || !isFinite(n)) return '—'; if (n < 1000) return String(Math.round(n)); if (n < 1000000) return `${Math.round(n/1000)}k`; return `${(n/1000000).toFixed(1)}M`; }
  const home = (() => { try { const env=(process.env.HERMES_HOME||'').trim(); return env ? env.replace(/\+/g,'/') : `${(process.env.USERPROFILE||process.env.HOME||'').trim()}/.hermes`; } catch { return ''; } })()
  const DEFAULT_MODELS = ['openrouter/free','deepseek/deepseek-chat','deepseek/deepseek-reasoner','anthropic/claude-3.5-sonnet','google/gemini-2.5-pro','meta-llama/llama-3.3-70b-instruct']
  async function loadSessionForModel(modelName) {
    try { const { DatabaseSync } = await import('node:sqlite'); const db = new DatabaseSync(`${home}/state.db`, { readOnly: true }); try { const sid=(process.env.HERMES_SESSION_ID||'').trim(); const w = sid ? `WHERE session_id = ? AND model = ?` : `WHERE model = ?`; const p = sid ? [sid, modelName] : [modelName]; const row = db.prepare(`SELECT SUM(estimated_cost_usd) AS spend, SUM(input_tokens+output_tokens) AS tok, SUM(api_call_count) AS calls, SUM(cache_read_tokens) AS cr FROM session_model_usage ${w}`).get(...p); db.close(); return row || { spend:0, tok:0, calls:0, cr:0 }; } catch(e){ db.close(); return {spend:0,tok:0,calls:0,cr:0}; } } catch { return {spend:0,tok:0,calls:0,cr:0}; }
  }
  const app = {}
  defineWidgetApp({
    id: 'model-cost', help: 'model picker with cost beside selection; footer updates on cursor/selection', mode: 'modal',
    init: async (arg='') => { const d = await loadSessionForModel(DEFAULT_MODELS[0]); return { idx:0, models:DEFAULT_MODELS, detail:d, arg:(arg||'').trim(), loaded:true }; },
    reduce: (state,{ch,key}) => {
      if (key.escape || ch==='q') return null
      if (ch==='j' || key.down || ch==='\x1b[B') { const next={...state, idx: Math.min(state.idx+1, (state.models.length||1)-1)}; void loadSessionForModel(state.models[next.idx]||'').then(d=>{try{updateWidget(app,s=>({...s,detail:d}))}catch{}}); return next }
      if (ch==='k' || key.up || ch==='\x1b[A') { const prev={...state, idx: Math.max(state.idx-1,0)}; void loadSessionForModel(state.models[prev.idx]||'').then(d=>{try{updateWidget(app,s=>({...s,detail:d}))}catch{}}); return prev }
      return state
    },
    render: ({state,t}) => {
      const sel = state.models[state.idx]||'—'; const price = PRICE_1K[sel]||{in:0,out:0}; const costText = price.in+price.out>0 ? `in $${price.in}/1k  out $${price.out}/1k` : 'free / n/a';
      const sessionSpending = state.detail?.spend||0, sessionTok = state.detail?.tok||0, sessionCalls = state.detail?.calls||0;
      const leftLines = state.models.map((m,i)=>{ const isSel=i===state.idx; return `${isSel?'▶':' '} ${(m||'').slice(0,28).padEnd(28,' ')}`; }).join('\n');
      const rightLines = [`Rate/1K: ${costText}`, `Session USD: ${usd(sessionSpending)}`, `Session tok: ${kfmt(sessionTok)}`, `Session calls: ${sessionCalls}`, `Cache tok: ${state.detail?.cr!==undefined?kfmt(state.detail.cr):'—'}`];
      const footerText = `FOOTER · model=${sel} · rate=${costText} · session_spend=${usd(sessionSpending)} · tok=${kfmt(sessionTok)} · calls=${sessionCalls}`;
      const content = [`MODEL PICKER  (/model-cost  ↑↓ pick  q/esc close)`, `────────────────────────────────────────────────────`, `   MODEL (cursor →)             COST`, `-----------------------------------------------`, leftLines, `-----------------------------------------------`, ...rightLines.map(s=>`  ${s}`), `-----------------------------------------------`, footerText].join('\n');
      return h(Dialog, { width: WIDTH, title: 'model-cost', hint: 'cursor over model → footer shows rate + session burn' }, h(Text, { color: t.color.label }, content));
    },
  });
}
