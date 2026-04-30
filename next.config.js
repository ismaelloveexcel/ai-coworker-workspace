const withBundleAnalyzer = require('@next/bundle-analyzer')({
  enabled: process.env.ANALYZE === 'true',
  openAnalyzer: false,
});

/** @type {import('next').NextConfig} */
const nextConfig = {
  compress: true,

  async headers() {
    return [
      {
        // Content-Security-Policy for all routes.
        // 'unsafe-inline' and 'unsafe-eval' are currently required by Next.js
        // for its runtime inline scripts and style injection. Once the app
        // adopts Next.js nonce-based CSP (middleware + generateBuildId nonce),
        // remove these directives and replace with 'nonce-{nonce}'.
        source: '/(.*)',
        headers: [
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self'",
              // TODO: replace 'unsafe-inline'/'unsafe-eval' with nonces once
              //       Next.js nonce middleware is configured.
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              "font-src 'self'",
              "connect-src 'self'",
              "frame-ancestors 'none'",
            ].join('; '),
          },
        ],
      },
      {
        // Immutable long-term cache for hashed Next.js static assets
        source: '/_next/static/:path*',
        headers: [
          {
            key: 'Cache-Control',
            value: 'public, max-age=31536000, immutable',
          },
        ],
      },
      {
        // Cache for Next.js optimized images
        source: '/_next/image',
        headers: [
          {
            key: 'Cache-Control',
            value: 'public, max-age=86400, stale-while-revalidate=604800',
          },
        ],
      },
      {
        // Cache for public directory static assets at root level (images, fonts, SVGs, etc.)
        source:
          '/:asset([\\w][\\w\\-.]*\\.(?:svg|png|jpg|jpeg|gif|ico|webp|avif|woff|woff2|ttf|otf|eot))',
        headers: [
          {
            key: 'Cache-Control',
            value: 'public, max-age=86400, stale-while-revalidate=604800',
          },
        ],
      },
    ];
  },
};

module.exports = withBundleAnalyzer(nextConfig);
