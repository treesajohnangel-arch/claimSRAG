import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router";
import { useSession } from "../auth/AuthContext";
import { ROLE_LABELS } from "../auth/permissions";
import { Icon, type IconName } from "./Icon";
import styles from "./AppLayout.module.css";

const NAVIGATION: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Overview", icon: "overview" },
  { to: "/policies", label: "Policies", icon: "policies" },
  { to: "/documents", label: "Documents", icon: "documents" },
  { to: "/search", label: "Search", icon: "search" },
  { to: "/ask", label: "Ask", icon: "ask" },
];

/** The ClaimsRAG mark: a page with a check, in the warm accent. */
export function Brand() {
  return (
    <span className={styles.brand}>
      <svg width="24" height="24" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
        <rect width="32" height="32" rx="8" fill="var(--accent)" />
        <path d="M10 7h9l5 5v13H10z" fill="none" stroke="#050607" strokeWidth="2.2" strokeLinejoin="round" />
        <path d="M13.5 18.5l2.5 2.5 4.5-5" fill="none" stroke="#050607" strokeWidth="2.2" strokeLinecap="round"
          strokeLinejoin="round" />
      </svg>
      ClaimsRAG
    </span>
  );
}

export function AppLayout() {
  const session = useSession();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => setMenuOpen(false), [location.pathname]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menuOpen]);

  return (
    <div className={styles.shell}>
      <a className={styles.skip} href="#main">
        Skip to content
      </a>
      <header className={styles.topbar}>
        <Brand />
        <button
          type="button"
          className={styles.menuButton}
          aria-expanded={menuOpen}
          aria-controls="app-navigation"
          onClick={() => setMenuOpen((open) => !open)}
        >
          <Icon name={menuOpen ? "close" : "menu"} />
          <span className="visually-hidden">{menuOpen ? "Close menu" : "Open menu"}</span>
        </button>
      </header>

      <aside id="app-navigation" className={`${styles.rail} ${menuOpen ? styles.railOpen : ""}`}>
        <div className={styles.railBrand}>
          <Brand />
        </div>
        <nav aria-label="Main">
          <ul className={styles.nav}>
            {NAVIGATION.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => `${styles.navLink} ${isActive ? styles.active : ""}`}
                >
                  <Icon name={item.icon} size={18} />
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className={styles.context}>
          <div className={styles.workspace}>
            <span className="data-label">Workspace</span>
            <span className={styles.workspaceName}>{session.company.name}</span>
            <span className={styles.workspaceCode}>{session.company.slug}</span>
            <span className={styles.private}>
              <Icon name="lock" size={13} />
              Private to your company
            </span>
          </div>
          <div className={styles.person}>
            <span className={styles.personName}>{session.user.full_name}</span>
            <span className={styles.personMeta}>{ROLE_LABELS[session.user.role]}</span>
            <span className={styles.personMeta}>{session.user.email}</span>
          </div>
          <p className={styles.demoNote}>Demo workspace: opened without sign-in.</p>
        </div>
      </aside>

      {menuOpen ? (
        // A pointer convenience only: keyboard users close the menu with Escape or the menu button.
        <div className={styles.scrim} aria-hidden="true" onClick={() => setMenuOpen(false)} />
      ) : null}

      <main id="main" className={styles.main} tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
