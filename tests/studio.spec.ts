import { expect, test } from '@playwright/test';
import { parse } from 'yaml';

test('visual editing, YAML validation, undo, and local persistence', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  await page.goto('/');
  await expect(page).toHaveTitle('LinkML Studio - OSDU Ready');
  await expect(page.locator('.brand')).toContainText('OSDU Ready');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await expect(page.locator('.class-card')).toHaveCount(2);
  await page.getByRole('textbox', { name: 'New class name' }).fill('Study');
  await page.getByRole('button', { name: '+ Class', exact: true }).click();
  await expect(page.locator('.class-card')).toHaveCount(3);
  await expect(page.getByText('LinkML valid · saved locally', { exact: true })).toBeVisible();
  await page.getByLabel('Selected class').selectOption('Study');
  await page.getByPlaceholder('e.g. email').fill('title');
  await page.getByRole('button', { name: '+ Add property', exact: true }).click();
  await expect(page.locator('.class-card').filter({ hasText: 'Study' }).getByText('title', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(page.locator('.class-card').filter({ hasText: 'Study' }).getByText('title', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'YAML', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'LinkML YAML editor' });
  const valid = await editor.inputValue();
  await editor.fill('classes: [');
  await page.getByRole('button', { name: 'Validate & apply' }).click();
  await expect(page.getByRole('alert')).toContainText('YAML parsing error');
  await expect(page.getByRole('button', { name: 'Export YAML' })).toBeDisabled();
  await page.getByRole('button', { name: 'Discard edits' }).click();
  await expect(editor).toHaveValue(valid);
  await page.reload();
  await expect(page.getByText('Restored local draft · LinkML valid', { exact: true })).toBeVisible();
  await expect(page.locator('.class-card')).toHaveCount(3);
  expect(browserErrors).toEqual([]);
});

test('AI proposal is reviewed before application and uses current schema', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ json: { status: 'ok', ai_configured: true } }));
  await page.route('**/api/chat', async route => {
    const request = route.request().postDataJSON();
    expect(request.yaml).toContain('Person:');
    expect(request.messages[0].role).toBe('user');
    await route.fulfill({ json: {
      message: 'I suggest adding a Study class.',
      questions: ['What uniquely identifies a study?'],
      proposed_yaml: `${request.yaml}\n  Study:\n    description: A research study.\n`,
      validation_errors: [],
    } });
  });
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await expect(page.locator('.privacy')).toContainText('Microsoft Foundry configured');
  await page.getByRole('textbox', { name: 'Message to the modeling assistant' }).fill('Add a Study class.');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByText('What uniquely identifies a study?')).toBeVisible();
  await expect(page.locator('.class-card')).toHaveCount(2);
  await page.getByRole('button', { name: 'Apply proposed changes' }).click();
  await expect(page.locator('.class-card')).toHaveCount(3);
  await expect(page.getByText('LinkML valid · saved locally', { exact: true })).toBeVisible();
});

test('missing Foundry configuration is explained and chat remains disabled', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ json: {
    status: 'ok', ai_configured: false, ai_provider: 'Microsoft Foundry',
    ai_configuration_error: 'Set FOUNDRY_API_KEY in backend/.env and restart the backend to enable Microsoft Foundry.',
  } }));
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await expect(page.locator('.privacy')).toContainText('FOUNDRY_API_KEY');
  await page.getByRole('textbox', { name: 'Message to the modeling assistant' }).fill('Help me model OSDU.');
  await expect(page.getByRole('button', { name: 'Send message' })).toBeDisabled();
  await expect(page.getByRole('button', { name: '+ Class', exact: true })).toBeEnabled();
});

test('connects classes visually and prevents deletion of referenced classes', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  const source = page.locator('.react-flow__node').filter({ hasText: 'Organization' }).filter({ hasNotText: 'Person' }).locator('.react-flow__handle-right');
  const target = page.locator('.react-flow__node').filter({ hasText: 'Person' }).locator('.react-flow__handle-left');
  await source.dragTo(target);
  await expect(page.getByText('LinkML valid · saved locally', { exact: true })).toBeVisible();
  await page.getByLabel('Selected class').selectOption('Organization');
  const property = page.locator('.slot-editor').filter({ has: page.locator('summary').filter({ hasText: /^person/ }) });
  await property.locator('summary').click();
  await property.getByLabel('Multiple values').click();
  await expect(property.getByLabel('Multiple values')).toBeChecked();
  await page.getByLabel('Selected class').selectOption('Person');
  await page.getByRole('button', { name: 'Delete class', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Cannot delete Person');
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export JSON Schema', exact: true }).click();
  expect((await downloadEvent).suggestedFilename()).toBe('research.schema.json');
});

test('imports a schema larger than four megabytes without truncation', async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'YAML', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'LinkML YAML editor' });
  const original = await editor.inputValue();
  const large = original.replace(/^description:.*$/m, `description: ${'schema details '.repeat(300_000)}`);
  const buffer = Buffer.from(large, 'utf-8');
  expect(buffer.byteLength).toBeGreaterThan(4 * 1024 * 1024);
  await page.locator('input[type="file"]').setInputFiles({ name: 'large.yaml', mimeType: 'text/yaml', buffer });
  await expect(page.getByText('LinkML valid · saved locally', { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole('button', { name: 'Export YAML', exact: true })).toBeEnabled();
  expect(await editor.inputValue()).toBe(large);
  await page.getByRole('button', { name: 'Visual builder', exact: true }).click();
  await expect(page.locator('.class-card')).toHaveCount(2);
});

test('loads OSDU-style class names and retains quantity-kind units with warnings', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  const text = `id: https://example.org/osdu
name: osdu_example
prefixes:
  osdu: https://example.org/osdu/
  linkml: https://w3id.org/linkml/
default_prefix: osdu
default_range: string
imports:
  - linkml:types
classes:
  master-data/Well:
    attributes:
      depth:
        range: float
        unit:
          has_quantity_kind: https://example.org/length
`;
  await page.locator('input[type="file"]').setInputFiles({
    name: 'osdu_example.yaml', mimeType: 'text/yaml', buffer: Buffer.from(text),
  });
  await expect(page.getByText('Loaded with 1 validation warnings', { exact: true })).toBeVisible();
  await page.locator('.warnings > summary').click();
  await expect(page.locator('.warnings')).toContainText('master-data/Well.attributes.depth.unit');
  await expect(page.getByRole('textbox', { name: 'LinkML YAML editor' })).toHaveValue(text);
  await page.getByRole('button', { name: 'Visual builder', exact: true }).click();
  await expect(page.locator('.class-card')).toContainText('master-data/Well');
  await page.reload();
  await expect(page.getByText('Loaded with 1 validation warnings', { exact: true })).toBeVisible();
});

test('pending validation can be canceled or discarded without applying late responses', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'YAML', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'LinkML YAML editor' });
  const original = await editor.inputValue();
  const releases: Array<() => void> = [];
  await page.route('**/api/validate', async route => {
    const text = route.request().postDataJSON().yaml;
    await new Promise<void>(resolve => releases.push(resolve));
    await route.fulfill({ json: { valid: true, strict_valid: true, warnings: [], errors: [], schema: parse(text), ranges: [] } });
  });
  const pending = original.replace('name: research', 'name: pending');
  await editor.fill(pending);
  await page.getByRole('button', { name: 'Validate & apply' }).click();
  await expect(page.locator('.operation-banner')).toContainText('Validating and applying schema');
  await expect(page.getByRole('button', { name: 'Processing...', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Discard edits' })).toBeEnabled();
  await expect.poll(() => releases.length).toBe(1);
  await page.getByRole('button', { name: 'Cancel operation' }).click();
  await expect(editor).toHaveValue(pending);
  await expect(editor).toBeEnabled();
  await page.getByRole('button', { name: 'Validate & apply' }).click();
  await expect.poll(() => releases.length).toBe(2);
  releases[0]();
  await expect(page.getByRole('button', { name: 'Processing...', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Discard edits' }).click();
  releases[1]();
  await page.unrouteAll({ behavior: 'wait' });
  await expect(editor).toHaveValue(original);
  await expect(page.locator('.project-name')).toContainText('research');
  await expect(page.getByRole('button', { name: 'Validate & apply' })).toBeEnabled();
  await editor.fill(pending);
  await page.getByRole('button', { name: 'Validate & apply' }).click();
  await expect(page.locator('.project-name')).toContainText('pending');
});

test('validation timeout releases the controls and preserves the YAML draft', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('LinkML valid', { exact: true })).toBeVisible();
  await page.clock.install();
  await page.getByRole('button', { name: 'YAML', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'LinkML YAML editor' });
  const text = (await editor.inputValue()).replace('name: research', 'name: timeout_draft');
  let release!: () => void;
  await page.route('**/api/validate', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ status: 503, json: { detail: 'Unavailable' } });
  });
  await editor.fill(text);
  await page.getByRole('button', { name: 'Validate & apply' }).click();
  await expect(page.locator('.operation-banner')).toBeVisible();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.clock.runFor(300_001);
  await expect(page.getByRole('alert')).toContainText('timed out after 5 minutes');
  await expect(page.getByRole('button', { name: 'Validate & apply' })).toBeEnabled();
  await expect(editor).toHaveValue(text);
  release();
  await page.unrouteAll({ behavior: 'wait' });
});
