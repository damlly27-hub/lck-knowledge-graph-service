import "./globals.css";

export const metadata = {
  title: "LCK Knowledge Graph Dashboard",
  description: "Neo4j 지식그래프 기반 LCK 질의응답 시스템",
};

export default function RootLayout({ children }) {
  return (
    <html lang="ko" data-theme="light">
      <body>{children}</body>
    </html>
  );
}
