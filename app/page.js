import Logo from '../components/Logo';

export default function Home() {
  return (
    <main
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '1.5rem',
      }}
    >
      <Logo width={160} height={54} />
      <h1 style={{ marginTop: '1.5rem', fontSize: '2rem', fontWeight: 700 }}>
        AI Coworker
      </h1>
      <p style={{ color: '#94a3b8', maxWidth: '32rem', textAlign: 'center' }}>
        Autonomous coding agent platform — submit a task and let the agent
        write, commit, and open a PR for you.
      </p>
    </main>
  );
}
