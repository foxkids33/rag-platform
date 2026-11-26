import { createContext, ReactNode, useContext } from "react";
import { runtimeConfig } from "./runtime-config";

export type AuthUser = {
  subject: string;
  roles: readonly string[];
  username?: string;
  display_name?: string | null;
  is_admin?: boolean;
};

const AuthUserContext = createContext<AuthUser | null>(null);

export function AuthUserProvider({ user, children }: { user: AuthUser; children: ReactNode }) {
  return <AuthUserContext.Provider value={user}>{children}</AuthUserContext.Provider>;
}

export function useAuthUser(): AuthUser | null {
  return useContext(AuthUserContext);
}

export function useIsAdmin(): boolean {
  const user = useAuthUser();
  return !runtimeConfig.authEnabled || Boolean(user?.is_admin ?? user?.roles.includes("rag-admin"));
}

export function useCanUseConfluence(): boolean {
  const user = useAuthUser();
  return !runtimeConfig.authEnabled || runtimeConfig.authMode !== "password" || Boolean(user);
}
