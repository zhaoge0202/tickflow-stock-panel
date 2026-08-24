# 前端二次开发目录

在独立子目录中创建 `extension.tsx`，构建时会自动发现，无需修改核心路由和导航文件。

```text
frontend/src/custom/<namespace>/extension.tsx
```

以 [`_template/extension.tsx.example`](_template/extension.tsx.example) 为起点，并遵循 [`docs/secondary-development.md`](../../../docs/secondary-development.md)。模板文件不会参与构建。
