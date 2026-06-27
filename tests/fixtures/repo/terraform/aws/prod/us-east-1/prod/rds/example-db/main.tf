resource "aws_db_instance" "example" {
  identifier        = "example-db"
  engine            = "postgres"
  instance_class    = "db.t3.micro"
  allocated_storage = 20
  username          = "admin"
  skip_final_snapshot = true
}
