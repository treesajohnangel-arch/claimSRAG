import type { Role } from "../api/types";

// What the interface offers each role. It mirrors app/rbac.py ROLE_PERMISSIONS only to hide controls a role cannot
// use: hiding is a courtesy, not security. The backend checks every request and answers 403 otherwise, which the
// screens handle.

const MANAGES_POLICIES: Record<Role, boolean> = { COMPANY_ADMIN: true, STAFF: false };

export function canManagePolicies(role: Role): boolean {
  return MANAGES_POLICIES[role] ?? false;
}

export const ROLE_LABELS: Record<Role, string> = { COMPANY_ADMIN: "Company admin", STAFF: "Staff" };
