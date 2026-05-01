export default function FormGroup({ label, children, htmlFor }) {
  return (
    <div className="form-group">
      {label && <label htmlFor={htmlFor}>{label}</label>}
      {children}
    </div>
  )
}
