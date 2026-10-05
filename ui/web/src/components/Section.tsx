export default function Section({
  title,
  aside,
  children,
}: {
  title: string
  aside?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {aside && <div className="section-aside">{aside}</div>}
      </div>
      {children}
    </section>
  )
}
