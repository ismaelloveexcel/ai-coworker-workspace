import './globals.css';

export const metadata = {
  title: 'AI Coworker',
  description: 'Autonomous coding agent platform powered by Claude',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
