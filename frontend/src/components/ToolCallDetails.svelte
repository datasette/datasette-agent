<script lang="ts">
  let {
    name,
    arguments: args,
    pending = false,
  }: {
    name: string;
    arguments: string | Record<string, unknown>;
    pending?: boolean;
  } = $props();

  const formatted = $derived(
    typeof args === "string"
      ? args
      : JSON.stringify(args, null, 2),
  );
</script>

<details class="agent-tool-call" class:pending>
  <summary>Tool: {name}</summary>
  <pre>{formatted}</pre>
</details>
