// Group nearby markers in projected pixels; groups split naturally as the map zooms in.
// A point joins the first group whose anchor is within `radius`, so neighbours across a grid
// line are grouped too. With radius 0 (maximum zoom) only hotels with identical coordinates group.
export function groupMapPoints(points, radius = 44) {
  const groups = []
  for (const point of points) {
    const group = groups.find((item) => Math.hypot(item.x - point.x, item.y - point.y) <= radius)
    if (group) group.points.push(point)
    else groups.push({ x: point.x, y: point.y, points: [point] })
  }
  return groups.map((group) => group.points)
}
