/** 公开版入口（直通桩）：没有登录、没有用户管理，也没有对话 / SOP 剧本编辑 / 成本这几页，
 *  控制台就是 `App.tsx` 里写死的那些页面。打包公开版时用它覆盖 `index.tsx`，并删除本目录下的
 *  gate.tsx / Users.tsx 以及 pages/Chat.tsx、Cost.tsx、Playbook*.tsx。
 *
 *  会员版在 `index.tsx` 里通过 `memberNavItems()` 把那些页面注入侧边栏；这里返回空数组。 */
import type { FC, ReactNode } from "react";
import type { DictKey } from "../lib/i18n";

export type NavItem = { key: string; labelKey: DictKey; descKey: DictKey; page: FC; group: DictKey; after?: string };

export const AuthGate = ({ children }: { children: ReactNode }) => <>{children}</>;
export const UserMenu = () => null;
export const useMember = () => ({ enabled: false, user: null });
export const memberNavItems = (_user: unknown): NavItem[] => [];
