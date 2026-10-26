export const VIDEO_ASPECTS = ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9'];

// Use the same binary megapixels and alignment for drafts and saved job edits.
export function videoResolution(megapixels, widthRatio, heightRatio, alignment = 32) {
  const values = [megapixels, widthRatio, heightRatio, alignment].map(Number);
  if (values.some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Choose a positive resolution and aspect ratio.');
  const scale = Math.sqrt(values[0] * 1024 * 1024 / (values[1] * values[2]));
  const width = Math.max(alignment, Math.round(values[1] * scale / alignment) * alignment);
  const height = Math.max(alignment, Math.round(values[2] * scale / alignment) * alignment);
  if (![width, height].every(Number.isFinite)) throw new Error('Choose a smaller resolution and aspect ratio.');
  return { width, height };
}

export function videoAspect(width, height, alignment = 32) {
  width = Number(width); height = Number(height);
  if (!(width > 0 && height > 0)) return '';
  const megapixels = width * height / 1024 / 1024;
  return VIDEO_ASPECTS.find(aspect => {
    const size = videoResolution(megapixels, ...aspect.split(':'), alignment);
    return size.width === width && size.height === height;
  }) || `${width}:${height}`;
}
