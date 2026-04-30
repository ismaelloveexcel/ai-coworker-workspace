'use client';

import { useEffect, useState } from 'react';

const API = '/api';

export default function Dashboard() {
  const [tasks, setTasks] = useState([]);
  const [title, setTitle] = useState('');
  const [prompt, setPrompt] = useState('');
  const [repo, setRepo] = useState('');
  const [error, setError] = useState('');
  const [budgetCap, setBudgetCap] = useState(null);

  async function loadTasks() {
    const response = await fetch(`${API}/tasks?limit=50&offset=0`);
    if (!response.ok) return;
    const data = await response.json();
    setTasks(data.tasks || []);
    setBudgetCap(data.max_task_usd ?? null);
  }

  useEffect(() => {
    loadTasks();
  }, []);

  async function createTask() {
    setError('');
    if (!title.trim() || !prompt.trim()) {
      setError('Title and prompt are required.');
      return;
    }
    const headers = { 'Content-Type': 'application/json' };
    const apiKey = localStorage.getItem('api_key');
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    const response = await fetch(`${API}/tasks`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ title: title.trim(), prompt: prompt.trim(), repo_url: repo.trim() || null }),
    });
    if (response.status === 401) {
      const key = window.prompt('API key required:');
      if (key) localStorage.setItem('api_key', key);
      return;
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setError(String(body.detail || response.statusText));
      return;
    }
    const task = await response.json();
    setTasks([task, ...tasks]);
    setTitle('');
    setPrompt('');
    setRepo('');
  }

  async function retryTask(taskId) {
    const headers = {};
    const apiKey = localStorage.getItem('api_key');
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    const response = await fetch(`${API}/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST', headers });
    if (!response.ok) return;
    const task = await response.json();
    setTasks([task, ...tasks]);
  }

  return (
    <main style={{ minHeight: '100vh', background: '#0f172a', color: '#e2e8f0', padding: '1.5rem' }}>
      <div style={{ maxWidth: '64rem', margin: '0 auto', display: 'grid', gap: '1rem' }}>
        <header style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'center' }}>
          <h1 style={{ color: '#fff', fontSize: '1.5rem', fontWeight: 700, margin: 0 }}>AI Coworker</h1>
          <button onClick={loadTasks} style={buttonStyle}>Refresh</button>
        </header>

        <section style={panelStyle}>
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Task title" style={inputStyle} />
          <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe what the agent should do..." rows={4} style={inputStyle} />
          <input value={repo} onChange={(event) => setRepo(event.target.value)} placeholder="owner/repo (optional)" style={inputStyle} />
          <button onClick={createTask} style={primaryButtonStyle}>Run Agent</button>
          {error ? <p style={{ color: '#f87171', margin: 0 }}>{error}</p> : null}
        </section>

        <section style={{ display: 'grid', gap: '0.75rem' }}>
          {tasks.map((task) => {
            const spent = Number(task.usd_spent || 0).toFixed(4);
            const budget = budgetCap == null ? `$${spent}` : `$${spent} / $${Number(budgetCap).toFixed(2)}`;
            return (
              <article key={task.id} style={panelStyle}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
                  <div>
                    <h2 style={{ color: '#fff', fontSize: '1rem', margin: 0 }}>{task.title}</h2>
                    <p style={mutedStyle}>{task.id}</p>
                    <p style={mutedStyle}>Spend: {budget}</p>
                    {task.error ? <p style={{ color: '#fca5a5', margin: '0.25rem 0 0' }}>{task.error}</p> : null}
                  </div>
                  <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-start' }}>
                    {task.pr_url ? <a href={task.pr_url} style={{ color: '#60a5fa' }}>PR</a> : null}
                    <span>{task.status}</span>
                    {['failed', 'cancelled'].includes(task.status) ? <button onClick={() => retryTask(task.id)} style={buttonStyle}>Retry</button> : null}
                  </div>
                </div>
              </article>
            );
          })}
        </section>
      </div>
    </main>
  );
}

const panelStyle = { background: '#1e293b', borderRadius: '0.5rem', padding: '1rem', display: 'grid', gap: '0.75rem' };
const inputStyle = { width: '100%', boxSizing: 'border-box', background: '#334155', color: '#fff', border: '1px solid #475569', borderRadius: '0.5rem', padding: '0.625rem' };
const buttonStyle = { background: '#334155', color: '#e2e8f0', border: '1px solid #475569', borderRadius: '0.5rem', padding: '0.5rem 0.75rem', cursor: 'pointer' };
const primaryButtonStyle = { ...buttonStyle, background: '#2563eb', borderColor: '#2563eb', color: '#fff' };
const mutedStyle = { color: '#94a3b8', fontSize: '0.75rem', margin: '0.25rem 0 0' };
