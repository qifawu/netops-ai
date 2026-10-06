/** 扩展入口（直通桩）：这里没有登录、用户管理，控制台就是 `App.tsx` 里写死的那些页面；
 *  需要额外页面时，由 `index.tsx` 通过 `memberNavItems()` 把它们注入侧边栏，这里返回空数组。 */
import type { FC, ReactNode } from "react";
import type { DictKey } from "../lib/i18n";

export type NavItem = { key: string; labelKey: DictKey; descKey: DictKey; page: FC; group: DictKey; after?: string };

export const AuthGate = ({ children }: { children: ReactNode }) => <>{children}</>;
export const UserMenu = () => null;
export const useMember = () => ({ enabled: false, user: null });
export const memberNavItems = (_user: unknown): NavItem[] => [];
