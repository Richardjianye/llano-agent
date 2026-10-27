<template>
  <v-main>
    <v-container class="fill-height" fluid>
      <v-empty-state
        class="mx-auto"
        headline="页面不存在"
        icon="mdi-map-marker-question-outline"
        text="地址可能已失效或输入有误。"
      >
        <template #actions>
          <v-btn color="primary" :to="{ name: 'dashboard' }" variant="tonal">返回首页</v-btn>
        </template>
      </v-empty-state>
    </v-container>
  </v-main>
</template>
