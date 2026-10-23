import type { MetadataRoute } from 'next'

/** Add to Home Screen: the table opens full-screen, without browser chrome. */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'Fireside',
    short_name: 'Fireside',
    description: 'Gather your party. The table is open.',
    start_url: '/',
    display: 'standalone',
    // Matches the "Opening Fireside…" shell so launch has no flash.
    background_color: '#15191a',
    theme_color: '#15191a',
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png' },
      { src: '/icons/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  }
}
