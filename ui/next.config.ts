import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: process.env.BUILD_STANDALONE === 'true' ? 'standalone' : undefined,
  images: { remotePatterns: [] },
  transpilePackages: ['geist'],
  compiler: { removeConsole: process.env.NODE_ENV === 'production' }
};
export default nextConfig;
