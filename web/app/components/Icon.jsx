import "./icon.css";

const ICONS = {
  target: (
    <>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
    </>
  ),
  chain: (
    <>
      <path d="M9.5 14.5 14.5 9.5" />
      <path d="M7.2 17.8 5.6 19.4a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0" transform="translate(2 -3)" />
      <path d="m16.8 6.2 1.6-1.6a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" transform="translate(-2 3)" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="3" />
      <path d="M7 3v4M17 3v4M3 10h18" />
      <path d="M8 14h2M14 14h2M8 17h2M14 17h2" />
    </>
  ),
  article: (
    <>
      <path d="M5 3h11l3 3v15H5z" />
      <path d="M16 3v4h4M8 11h8M8 15h8M8 19h5" />
    </>
  ),
  newspaper: (
    <>
      <path d="M4 5h14v15H6a2 2 0 0 1-2-2z" />
      <path d="M18 8h2v10a2 2 0 0 1-2 2M7 9h4v4H7zM13 9h3M13 12h3M7 16h9" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m15.5 15.5 5 5" />
    </>
  ),
};

function iconKey(src) {
  const match = String(src || "").match(/([^/]+)\.[^.]+$/);
  return match ? match[1] : "article";
}

export default function Icon({ src, alt = "", size = 24 }) {
  const key = iconKey(src);
  return (
    <span className="icon" role={alt ? "img" : undefined} aria-label={alt || undefined} aria-hidden={alt ? undefined : true}>
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" focusable="false">
        {ICONS[key] || ICONS.article}
      </svg>
    </span>
  );
}
