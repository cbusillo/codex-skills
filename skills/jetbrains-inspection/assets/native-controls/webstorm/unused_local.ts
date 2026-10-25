export function sumWitness(values: number[]): number {
  const unusedWitness = values.length;
  return values.reduce((total, value) => total + value, 0);
}
