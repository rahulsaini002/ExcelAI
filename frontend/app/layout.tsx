import type { Metadata } from "next";
import "./globals.css";

// Note: we intentionally do NOT use next/font/google here. Fetching fonts from
// Google at build/dev time fails offline (and slowed the first load by ~12s).
// We use a system font stack via globals.css instead — instant and offline-safe.

export const metadata: Metadata = {
  title: "Sumio — conversational spreadsheets",
  description:
    "Describe what you want done to your spreadsheet in plain language and get the result back.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Set the theme class before first paint so there's no light/dark flash.
  const themeScript = `(function(){try{var t=localStorage.getItem('sumio_theme');if(t==='dark'||(!t&&window.matchMedia('(prefers-color-scheme: dark)').matches)){document.documentElement.classList.add('dark');}}catch(e){}})();`;

  return (
    // The inline theme script sets the `dark` class before React hydrates, so the
    // <html> attributes intentionally differ from SSR — suppress that warning.
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
