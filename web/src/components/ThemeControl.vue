<template>
  <div class="theme-control">
    <label for="theme-choice">主题</label>
    <span class="theme-swatch">
      <select id="theme-choice" v-model="theme.theme" aria-label="主题色">
        <option v-for="option in THEME_OPTIONS" :key="option.id" :value="option.id">{{ option.name }}</option>
      </select>
    </span>
    <button type="button" :aria-label="theme.appearance === 'dark' ? '切换浅色模式' : '切换深色模式'" @click="toggle">
      <PhSun v-if="theme.appearance === 'dark'" weight="bold" aria-hidden="true" />
      <PhMoon v-else weight="bold" aria-hidden="true" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { PhMoon, PhSun } from '@phosphor-icons/vue'
import { THEME_OPTIONS, useThemeStore } from '@/stores/theme'
const theme = useThemeStore()
function toggle() { theme.setAppearance(theme.appearance === 'dark' ? 'light' : 'dark') }
</script>
