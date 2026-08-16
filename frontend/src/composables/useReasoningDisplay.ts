/**
 * 思考过程显示偏好 Composable
 *
 * 控制 AI 回答中「思考过程」（ReasoningBlock）的显示方式：
 * - collapsed: 默认折叠（用户点击展开）
 * - expanded:  默认展开
 * - hidden:    不显示
 *
 * 偏好持久化到 localStorage（仿 useTheme 范式），模块级单例，
 * 所有组件共享同一状态。
 */

import { ref, type Ref } from 'vue'

export type ReasoningDisplayMode = 'collapsed' | 'expanded' | 'hidden'

const REASONING_DISPLAY_KEY = 'CountBot-reasoning-display'
const VALID_MODES: ReasoningDisplayMode[] = ['collapsed', 'expanded', 'hidden']

export function getStoredReasoningDisplay(): ReasoningDisplayMode {
    try {
        const stored = localStorage.getItem(REASONING_DISPLAY_KEY)
        if (stored && VALID_MODES.includes(stored as ReasoningDisplayMode)) {
            return stored as ReasoningDisplayMode
        }
    } catch {
        /* localStorage 不可用时忽略 */
    }
    return 'collapsed'
}

export function setStoredReasoningDisplay(mode: ReasoningDisplayMode): void {
    try {
        localStorage.setItem(REASONING_DISPLAY_KEY, mode)
    } catch {
        /* localStorage 不可用时忽略 */
    }
}

const displayMode = ref<ReasoningDisplayMode>(getStoredReasoningDisplay())

export function useReasoningDisplay() {
    /**
     * 当前显示模式
     */
    const mode: Ref<ReasoningDisplayMode> = displayMode

    /**
     * 设置显示模式并持久化
     */
    function setDisplayMode(next: ReasoningDisplayMode) {
        if (!VALID_MODES.includes(next)) {
            return
        }
        mode.value = next
        setStoredReasoningDisplay(next)
    }

    return {
        displayMode: mode,
        setDisplayMode,
    }
}
